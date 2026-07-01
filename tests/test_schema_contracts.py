from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

from core.director import DirectorDecisionError, validate_decision
from core.engine.run_record import (
    build_loop_session_record,
    build_director_failed_run_record,
    build_failed_run_record,
    build_run_record,
    build_workflow_failed_run_record,
    validate_run_result,
)
from core.schema import SchemaValidationError, validate_schema, validate_schema_consistency, validate_schema_keywords
from core.state.snapshot import SnapshotError, validate_snapshot
from core.validator import validate_chapter


class SchemaContractTest(unittest.TestCase):
    def test_schema_contracts_match_standalone_schemas(self) -> None:
        checked = validate_schema_consistency()

        self.assertEqual(8, len(checked))
        self.assertIn(
            {
                "source": str(Path("schemas/director_decision.schema.json")),
                "mirror": str(Path("core/director/schema.json")),
            },
            checked,
        )
        self.assertIn(
            {
                "source": "trace_event.schema.json",
                "embedded_in": "run_record.schema.json",
                "path": "properties.trace.items",
            },
            checked,
        )
        self.assertIn(
            {
                "source": "workflow_plan.schema.json",
                "embedded_in": "run_record.schema.json",
                "path": "properties.workflow_plan",
            },
            checked,
        )
        self.assertIn(
            {
                "source": "state_update_audit.schema.json",
                "embedded_in": "run_record.schema.json",
                "path": "properties.state_update",
            },
            checked,
        )

    def test_legacy_director_schema_asset_matches_runtime_schema(self) -> None:
        runtime_schema = json.loads(Path("schemas/director_decision.schema.json").read_text(encoding="utf-8"))
        legacy_schema = json.loads(Path("core/director/schema.json").read_text(encoding="utf-8"))

        self.assertEqual(runtime_schema, legacy_schema)

    def test_schema_validator_rejects_missing_required_key(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema({"ok": True, "checks": []}, "validation_result.schema.json")

    def test_schema_validator_rejects_unknown_closed_property(self) -> None:
        decision = {
            "chapter_index": 1,
            "goal": "continue_existing_arc",
            "actions": ["generate_chapter", "validate"],
            "validation_focus": ["logic"],
            "max_repair_attempts": 1,
            "notes": [],
            "extra": "not allowed",
        }

        with self.assertRaises(DirectorDecisionError):
            validate_decision(decision)

    def test_schema_validator_rejects_unsupported_schema_keyword(self) -> None:
        with self.assertRaisesRegex(SchemaValidationError, "pattern is unsupported"):
            validate_schema_keywords(
                {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "pattern": "^[A-Z]",
                        }
                    },
                },
                "test.schema.json",
            )

    def test_schema_validator_rejects_unsupported_schema_shapes(self) -> None:
        with self.assertRaisesRegex(SchemaValidationError, "additionalProperties must be a boolean"):
            validate_schema_keywords(
                {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "test.schema.json",
            )

    def test_schema_validator_handles_nullable_object_union(self) -> None:
        envelope = {"run": {}, "validation": None}

        self.assertIs(envelope, validate_schema(envelope, "run_result.schema.json"))

    def test_director_audit_matches_schema(self) -> None:
        audit = {
            "mode": "model",
            "source": "core.director.model_director.ModelDirector",
            "model": "gpt-test",
            "status": "failed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "duration_ms": 1000,
            "error_type": "ModelCallError",
            "error_message": "OpenAI director call failed: timeout",
            "model_call": {
                "provider": "openai",
                "stage": "director_decision",
                "model": "gpt-test",
                "cause_type": "TimeoutError",
                "message": "OpenAI director call failed: timeout",
            },
        }

        self.assertIs(audit, validate_schema(audit, "director_audit.schema.json"))

    def test_director_audit_rejects_unknown_status(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "mode": "rule",
                    "source": "core.director.director.decide_next_step",
                    "model": None,
                    "status": "skipped",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:01+00:00",
                    "duration_ms": 1000,
                },
                "director_audit.schema.json",
            )

    def test_snapshot_schema_is_part_of_snapshot_validation(self) -> None:
        with self.assertRaises(SnapshotError):
            validate_snapshot(
                {
                    "chapter_index": 1,
                    "world_state": {"locations": []},
                    "characters": {},
                    "timeline": [],
                }
            )

    def test_validation_result_matches_schema(self) -> None:
        result = validate_chapter(
            {"chapter_index": 1, "world_state": {}, "characters": {}, "timeline": []},
            "The shelter faced danger as the team had to choose a costly rescue.",
            {"chapter_index": 1},
        )

        self.assertIs(result, validate_schema(result, "validation_result.schema.json"))

    def test_analysis_result_matches_schema(self) -> None:
        analysis = {
            "summary": "The team had to choose.",
            "events": [{"text": "The team had to choose."}],
            "character_changes": [
                {
                    "name": "Mira",
                    "status": "injured",
                    "text": "Mira was injured during the rescue.",
                }
            ],
            "world_changes": [{"type": "serum_focus", "text": "Serum remains narratively relevant."}],
            "new_locations": ["shelter"],
            "conflicts": ["danger"],
            "validation_ok": True,
        }

        self.assertIs(analysis, validate_schema(analysis, "analysis_result.schema.json"))

    def test_analysis_result_rejects_missing_summary(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "events": [],
                    "character_changes": [],
                    "world_changes": [],
                    "new_locations": [],
                    "conflicts": [],
                    "validation_ok": False,
                },
                "analysis_result.schema.json",
            )

    def test_memory_context_matches_schema(self) -> None:
        memory = {
            "source": "jsonl-outbox",
            "status": "ready",
            "items": [
                {
                    "id": "manual:location:shelter",
                    "type": "location",
                    "name": "shelter",
                    "source_run_id": "run-1",
                    "data": {"risk": "rising"},
                }
            ],
            "source_mappings": [
                {
                    "index": 0,
                    "source": "jsonl-outbox",
                    "memory_id": "manual:location:shelter",
                    "type": "location",
                    "name": "shelter",
                    "path": "data/memory_outbox.jsonl",
                    "line_number": 1,
                }
            ],
        }

        self.assertIs(memory, validate_schema(memory, "memory_context.schema.json"))

    def test_input_pack_metadata_matches_schema(self) -> None:
        metadata = {
            "kind": "chapter_input_pack",
            "chapter_index": 2,
            "chars": 512,
            "sections": [
                "chapter_index",
                "director_decision",
                "world_state",
                "characters",
                "timeline",
                "constraints",
                "runtime_memory_metadata",
                "memory_index",
                "recovery_context",
                "requirements",
            ],
            "decision": {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
            },
            "snapshot": {
                "world_state_keys": ["locations"],
                "character_count": 2,
                "timeline_count": 3,
                "constraint_count": 1,
                "memory_source": "notion-export",
                "memory_status": "ready",
                "memory_item_count": 4,
            },
            "memory_index": {
                "source": "notion-export",
                "status": "ready",
                "item_count": 4,
                "indexed_item_count": 4,
                "source_mapping_count": 4,
                "last_run_present": False,
            },
            "recovery_context": {
                "available": False,
                "source_run_id": None,
                "status": None,
                "problem_count": 0,
                "executed_checks": [],
                "skipped_checks": [],
                "repair_attempts": 0,
            },
        }

        self.assertIs(metadata, validate_schema(metadata, "input_pack_metadata.schema.json"))

    def test_input_pack_metadata_rejects_unknown_action(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "kind": "chapter_input_pack",
                    "chapter_index": 2,
                    "chars": 512,
                    "sections": ["chapter_index"],
                    "decision": {
                        "goal": "continue_existing_arc",
                        "actions": ["archive_notes"],
                        "validation_focus": ["logic"],
                        "max_repair_attempts": 1,
                    },
                    "snapshot": {
                        "world_state_keys": [],
                        "character_count": 0,
                        "timeline_count": 0,
                        "constraint_count": 0,
                        "memory_source": None,
                        "memory_status": None,
                        "memory_item_count": 0,
                    },
                    "memory_index": {
                        "source": None,
                        "status": None,
                        "item_count": 0,
                        "indexed_item_count": 0,
                        "source_mapping_count": 0,
                        "last_run_present": False,
                    },
                    "recovery_context": {
                        "available": False,
                        "source_run_id": None,
                        "status": None,
                        "problem_count": 0,
                        "executed_checks": [],
                        "skipped_checks": [],
                        "repair_attempts": 0,
                    },
                },
                "input_pack_metadata.schema.json",
            )

    def test_snapshot_builder_audit_matches_schema(self) -> None:
        audit = {
            "source": "notion-export",
            "status": "ready",
            "item_count": 2,
            "applied_count": 1,
            "skipped_count": 1,
            "deduplicated_count": 1,
            "applied_type_counts": [{"type": "location", "count": 1}],
            "skipped_type_counts": [{"type": "constraint", "count": 1}],
            "skipped_reason_counts": [{"reason_code": "duplicate_memory", "count": 1}],
            "skipped_severity_counts": [{"severity": "low", "count": 1}],
            "skipped_blocking_count": 0,
            "applied_items": [
                {
                    "index": 0,
                    "type": "location",
                    "name": "shelter",
                    "id": "manual:location:shelter",
                    "operation": "upsert_location",
                    "target": "world_state.locations.shelter",
                    "source_mapping": {
                        "index": 0,
                        "source": "notion-export",
                        "memory_id": "manual:location:shelter",
                        "type": "location",
                        "name": "shelter",
                        "page_id": "page-1",
                        "page_url": "https://notion.test/page-1",
                        "page_index": 0,
                    },
                }
            ],
            "skipped_items": [
                {
                    "index": 1,
                    "type": "constraint",
                    "name": None,
                    "id": None,
                    "reason": "duplicate",
                    "reason_code": "duplicate_memory",
                    "severity": "low",
                    "category": "deduplication",
                    "blocking": False,
                    "source_mapping": {
                        "index": 1,
                        "source": "notion-export",
                        "memory_id": None,
                        "type": "constraint",
                        "name": None,
                        "page_id": "page-2",
                        "page_url": "https://notion.test/page-2",
                        "page_index": 1,
                    },
                }
            ],
        }

        self.assertIs(audit, validate_schema(audit, "snapshot_builder_audit.schema.json"))

    def test_snapshot_builder_audit_rejects_unknown_field(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "source": "test",
                    "status": "ready",
                    "item_count": 0,
                    "applied_count": 0,
                    "skipped_count": 0,
                    "deduplicated_count": 0,
                    "applied_type_counts": [],
                    "skipped_type_counts": [],
                    "skipped_reason_counts": [],
                    "skipped_severity_counts": [],
                    "skipped_blocking_count": 0,
                    "applied_items": [],
                    "skipped_items": [],
                    "unexpected": True,
                },
                "snapshot_builder_audit.schema.json",
            )

    def test_workflow_plan_matches_schema(self) -> None:
        plan = {
            "goal": "continue_existing_arc",
            "actions": ["generate_chapter", "validate", "repair_if_needed"],
            "steps": [
                {
                    "index": 1,
                    "action": "generate_chapter",
                    "requires": [],
                    "produces": ["chapter"],
                    "purpose": "Generate draft chapter prose from the input pack.",
                    "mode": "required",
                    "skippable": False,
                    "skip_condition": None,
                    "failure_policy": "fail_run",
                },
                {
                    "index": 2,
                    "action": "validate",
                    "requires": ["chapter"],
                    "produces": ["validation"],
                    "purpose": "Check continuity, spatial, and logic constraints selected by the Director.",
                    "mode": "required",
                    "skippable": False,
                    "skip_condition": None,
                    "failure_policy": "fail_run",
                },
                {
                    "index": 3,
                    "action": "repair_if_needed",
                    "requires": ["chapter", "validation"],
                    "produces": ["chapter", "validation"],
                    "purpose": "Repair failed validation problems within the Director repair budget.",
                    "mode": "conditional",
                    "skippable": True,
                    "skip_condition": "Runs only when validation is not ok and max_repair_attempts is greater than 0.",
                    "failure_policy": "fail_run",
                },
            ],
            "validation_focus": ["logic"],
            "max_repair_attempts": 2,
            "recovery": False,
        }

        self.assertIs(plan, validate_schema(plan, "workflow_plan.schema.json"))

    def test_workflow_plan_rejects_unknown_action(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "goal": "continue_existing_arc",
                    "actions": ["generate_chapter", "archive_notes"],
                    "steps": [
                        {
                            "index": 1,
                            "action": "archive_notes",
                            "requires": [],
                            "produces": ["chapter"],
                            "purpose": "Archive notes.",
                        }
                    ],
                    "validation_focus": [],
                    "max_repair_attempts": 0,
                    "recovery": False,
                },
                "workflow_plan.schema.json",
            )

    def test_state_update_audit_matches_schema(self) -> None:
        audit = {
            "applied": True,
            "chapter_index": 2,
            "next_chapter_index": 3,
            "timeline_added": 1,
            "character_update_count": 1,
            "location_update_count": 1,
            "world_change_count": 1,
            "memory_update_count": 4,
            "memory_update_types": [
                {"type": "character", "count": 1},
                {"type": "location", "count": 1},
                {"type": "timeline_event", "count": 2},
            ],
            "analysis_validation_ok": True,
        }

        self.assertIs(audit, validate_schema(audit, "state_update_audit.schema.json"))

    def test_state_update_audit_rejects_unknown_type(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "applied": True,
                    "chapter_index": 2,
                    "next_chapter_index": 3,
                    "timeline_added": 1,
                    "character_update_count": 0,
                    "location_update_count": 0,
                    "world_change_count": 0,
                    "memory_update_count": 1,
                    "memory_update_types": [{"type": "plot_seed", "count": 1}],
                    "analysis_validation_ok": True,
                },
                "state_update_audit.schema.json",
            )

    def test_trace_event_matches_schema(self) -> None:
        event = {
            "action": "repair_if_needed",
            "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "chapter_chars": 128,
            "repair_attempts": 1,
            "plan_step_index": 4,
            "plan_step_mode": "conditional",
            "plan_failure_policy": "fail_run",
            "model_stage": "scene_repair",
            "model_provider": "openai",
            "model_name": "gpt-test",
            "model_invocation": "model",
            "validation_ok": True,
            "problem_count": 0,
            "skipped": False,
            "repair_plan": {
                "problem_count": 1,
                "blocking_problem_count": 1,
                "warning_count": 0,
                "severity_counts": [{"severity": "high", "count": 1}],
                "risk_level": "high",
                "repair_budget": 2,
                "attempt": 1,
                "deterministic_step_count": 1,
                "manual_review_count": 0,
                "actions": ["add_conflict_signal"],
                "recovery": {
                    "available": True,
                    "source_run_id": "chapter_1_test",
                    "source_status": "rejected",
                    "source_problem_codes": ["missing_conflict_marker"],
                    "repeated_problem_codes": ["missing_conflict_marker"],
                    "unresolved_problem_codes": ["missing_conflict_marker"],
                    "new_problem_codes": [],
                    "skipped_checks": ["continuity", "spatial"],
                    "previous_repair_attempts": 1,
                    "previous_repair_risk_level": "high",
                    "previous_manual_review_count": 0,
                    "repair_stalled": True,
                    "repair_introduced_new_problems": False,
                    "repair_budget_exhausted": True,
                    "failure_modes": [
                        "previous_problem_repeated",
                        "previous_repair_stalled",
                        "previous_validation_skipped",
                        "previous_repair_budget_exhausted",
                    ],
                },
                "steps": [],
            },
            "repair_deltas": [
                {
                    "attempt": 1,
                    "before_ok": False,
                    "after_ok": True,
                    "before_problem_count": 1,
                    "after_problem_count": 0,
                    "before_problem_codes": ["missing_conflict_marker"],
                    "after_problem_codes": [],
                    "resolved_problem_codes": ["missing_conflict_marker"],
                    "new_problem_codes": [],
                    "remaining_problem_codes": [],
                }
            ],
        }

        self.assertIs(event, validate_schema(event, "trace_event.schema.json"))

    def test_trace_event_rejects_unknown_skip_reason(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "action": "repair_if_needed",
                    "status": "completed",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:01+00:00",
                    "chapter_chars": 128,
                    "repair_attempts": 0,
                    "skipped": True,
                    "skip_reason": "not_needed",
                },
                "trace_event.schema.json",
            )

    def test_repair_plan_matches_schema(self) -> None:
        plan = {
            "problem_count": 1,
            "blocking_problem_count": 1,
            "warning_count": 0,
            "severity_counts": [{"severity": "high", "count": 1}],
            "risk_level": "high",
            "repair_budget": 2,
            "attempt": 1,
            "deterministic_step_count": 1,
            "manual_review_count": 0,
            "actions": ["add_conflict_signal"],
            "recovery": {
                "available": False,
                "source_run_id": None,
                "source_status": None,
                "source_problem_codes": [],
                "repeated_problem_codes": [],
                "unresolved_problem_codes": [],
                "new_problem_codes": [],
                "skipped_checks": [],
                "previous_repair_attempts": 0,
                "previous_repair_risk_level": None,
                "previous_manual_review_count": 0,
                "repair_stalled": False,
                "repair_introduced_new_problems": False,
                "repair_budget_exhausted": False,
                "failure_modes": [],
            },
            "steps": [
                {
                    "index": 1,
                    "code": "missing_conflict_marker",
                    "message": "Missing conflict signal.",
                    "validator": "logic",
                    "severity": "high",
                    "blocking": True,
                    "repair_hint": "Add explicit danger, choice, threat, secret, cost, or conflict.",
                    "action": "add_conflict_signal",
                    "priority": 30,
                    "strategy": "Add explicit danger, choice, threat, secret, cost, or conflict.",
                    "parameters": {},
                }
            ],
        }

        self.assertIs(plan, validate_schema(plan, "repair_plan.schema.json"))

    def test_repair_plan_rejects_unknown_action(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "problem_count": 1,
                    "actions": ["rewrite_everything"],
                    "steps": [
                        {
                            "index": 1,
                            "code": "missing_conflict_marker",
                            "message": "Missing conflict signal.",
                            "validator": "logic",
                            "severity": "high",
                            "blocking": True,
                            "repair_hint": "Add conflict.",
                            "action": "rewrite_everything",
                            "priority": 30,
                            "strategy": "Rewrite without a registered strategy.",
                            "parameters": {},
                        }
                    ],
                },
                "repair_plan.schema.json",
            )

    def test_repair_plan_rejects_unknown_parameter_field(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "problem_count": 1,
                    "actions": ["add_required_term"],
                    "steps": [
                        {
                            "index": 1,
                            "code": "missing_required_constraint_term",
                            "message": "Missing serum.",
                            "severity": "high",
                            "blocking": True,
                            "repair_hint": "Add serum.",
                            "action": "add_required_term",
                            "priority": 50,
                            "strategy": "Mention the required term without resolving the constraint.",
                            "parameters": {"term": "serum", "unexpected": "field"},
                        }
                    ],
                },
                "repair_plan.schema.json",
            )

    def test_trace_event_rejects_unknown_action(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "action": "archive_notes",
                    "status": "completed",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:01+00:00",
                    "chapter_chars": 128,
                    "repair_attempts": 0,
                },
                "trace_event.schema.json",
            )

    def test_run_record_matches_schema(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={
                "source": "notion-export",
                "status": "ready",
                "items": [
                    {"id": "manual:location:shelter", "type": "location", "name": "shelter", "data": {}},
                    {"id": "chapter_1:timeline_event:summary", "type": "timeline_event", "name": "summary", "data": {}},
                ],
                "source_mappings": [
                    {
                        "index": 0,
                        "source": "notion-export",
                        "memory_id": "manual:location:shelter",
                        "type": "location",
                        "name": "shelter",
                        "path": "data/notion_memory.example.json",
                        "page_id": "page-1",
                        "page_url": "https://notion.test/page-1",
                        "page_index": 0,
                    },
                    {
                        "index": 1,
                        "source": "jsonl-outbox",
                        "memory_id": "chapter_1:timeline_event:summary",
                        "type": "timeline_event",
                        "name": "summary",
                        "path": "data/memory_outbox.jsonl",
                        "line_number": 3,
                    },
                ],
            },
            decision={
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="The shelter faced danger as the team had to choose a costly rescue.",
            validation={"ok": True, "problems": []},
            analysis={
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team had to choose."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team had to choose.",
            },
            repair_attempts=0,
            committed=True,
            workflow_trace=[
                {
                    "action": "generate_chapter",
                    "status": "completed",
                    "started_at": now.isoformat(),
                    "finished_at": now.isoformat(),
                    "chapter_chars": 64,
                    "repair_attempts": 0,
                },
                {
                    "action": "validate",
                    "status": "completed",
                    "started_at": now.isoformat(),
                    "finished_at": now.isoformat(),
                    "chapter_chars": 64,
                    "repair_attempts": 0,
                    "validation_ok": True,
                    "problem_count": 0,
                },
            ],
        )
        record["input_pack"]["artifact"] = {
            "path": "data/runs/input_packs/input_pack_0002_run.md",
            "chars": 5,
            "format": "markdown",
        }
        record["memory"]["writeback"] = {"written": 1, "skipped": 0}

        self.assertEqual("unknown", record["director"]["mode"])
        self.assertEqual(2, record["memory"]["source_mapping_count"])
        self.assertEqual(
            [{"source": "jsonl-outbox", "count": 1}, {"source": "notion-export", "count": 1}],
            record["memory"]["source_mapping_sources"],
        )
        self.assertEqual(2, record["memory"]["file_mapping_count"])
        self.assertEqual(1, record["memory"]["line_mapping_count"])
        self.assertEqual(1, record["memory"]["notion_page_mapping_count"])
        self.assertEqual(1, record["memory"]["notion_page_url_count"])
        self.assertEqual(0, record["snapshot_builder"]["chars"])
        self.assertIsNone(record["workflow_plan"])
        self.assertEqual(0, record["validation"]["deterministic_repair_count"])
        self.assertEqual(0, record["validation"]["manual_review_count"])
        self.assertEqual([], record["validation"]["repair_action_counts"])
        self.assertTrue(record["state_update"]["applied"])
        self.assertEqual(1, record["state_update"]["timeline_added"])
        self.assertIs(record, validate_schema(record, "run_record.schema.json"))

    def test_run_result_envelope_matches_schema(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="The shelter faced danger as the team had to choose a costly rescue.",
            validation={"ok": True, "problems": []},
            analysis={
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team had to choose."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team had to choose.",
            },
            repair_attempts=0,
            committed=True,
        )
        envelope = {
            "run": record,
            "decision": {
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            "workflow": ["generate_chapter", "validate"],
            "workflow_plan": None,
            "chapter": "The shelter faced danger as the team had to choose a costly rescue.",
            "validation": {"ok": True, "checks": [], "problems": []},
            "analysis": {
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team had to choose."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team had to choose.",
            },
            "state_update": record["state_update"],
            "snapshot": {"chapter_index": 3, "world_state": {}, "characters": {}, "timeline": []},
            "repair_attempts": 0,
            "committed": True,
            "memory_write": {
                "target": None,
                "written": 0,
                "skipped": True,
                "item_mappings": [],
                "verification": {"status": "not_applicable", "target": None, "reason": "no_updates"},
            },
        }

        self.assertIs(envelope, validate_run_result(envelope))

    def test_run_result_rejects_invalid_memory_writeback(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="The shelter faced danger as the team had to choose a costly rescue.",
            validation={"ok": True, "problems": []},
            analysis={
                "validation_ok": True,
                "conflicts": ["danger"],
                "events": [{"text": "The team had to choose."}],
                "character_changes": [],
                "world_changes": [],
                "new_locations": [],
                "summary": "The team had to choose.",
            },
            repair_attempts=0,
            committed=True,
        )
        envelope = {"run": record, "memory_write": {"written": 1, "verification": {"status": "verified", "target": "file"}}}

        with self.assertRaises(SchemaValidationError):
            validate_run_result(envelope)

    def test_run_result_envelope_rejects_unknown_top_level_fields(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema({"run": {}, "unexpected": True}, "run_result.schema.json")

    def test_loop_session_record_matches_schema(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session = build_loop_session_record(
            started_at=now,
            finished_at=now,
            requested_steps=2,
            completed_steps=2,
            stopped_reason="max_steps",
            persist=True,
            stop_on_rejection=True,
            runs=[
                {
                    "run": {
                        "id": "chapter_2_test",
                        "status": "committed",
                        "committed": True,
                        "chapter_index": 2,
                        "workflow": ["generate_chapter", "validate"],
                        "workflow_plan": {
                            "steps": [
                                {
                                    "index": 1,
                                    "action": "generate_chapter",
                                    "mode": "required",
                                    "failure_policy": "fail_run",
                                },
                                {
                                    "index": 2,
                                    "action": "validate",
                                    "mode": "required",
                                    "failure_policy": "fail_run",
                                },
                            ],
                        },
                        "trace": [
                            {
                                "action": "generate_chapter",
                                "plan_step_index": 1,
                                "plan_step_mode": "required",
                                "plan_failure_policy": "fail_run",
                            },
                            {
                                "action": "validate",
                                "plan_step_index": 2,
                                "plan_step_mode": "required",
                                "plan_failure_policy": "fail_run",
                            },
                        ],
                        "validation": {
                            "problem_codes": [],
                            "requested_focus": ["continuity", "spatial", "logic"],
                            "executed_checks": ["continuity", "spatial", "logic"],
                            "skipped_checks": [],
                        },
                        "repair_attempts": 0,
                    }
                },
                {
                    "run": {
                        "id": "chapter_3_test",
                        "status": "rejected",
                        "committed": False,
                        "chapter_index": 3,
                        "validation": {
                            "problem_codes": ["missing_conflict_marker"],
                            "requested_focus": ["logic"],
                            "executed_checks": ["logic"],
                            "skipped_checks": ["continuity", "spatial"],
                        },
                        "repair_attempts": 1,
                        "decision": {"goal": "recover_from_rejected_run"},
                        "recovery_context": {
                            "available": True,
                            "source_run_id": "chapter_2_test",
                            "source_status": "committed",
                            "source_chapter_index": 2,
                            "problem_codes": [],
                            "repair_stalled": False,
                            "repair_introduced_new_problems": False,
                            "repair_risk_level": None,
                            "repair_budget_exhausted": False,
                        },
                    }
                },
            ],
        )

        self.assertEqual("loop_20260101T000000000000Z", session["id"])
        self.assertEqual(1, session["committed_count"])
        self.assertEqual(1, session["rejected_count"])
        self.assertEqual("chapter_3_test", session["last_run_id"])
        self.assertEqual(["logic"], session["runs"][1]["requested_focus"])
        self.assertEqual(["continuity", "spatial"], session["runs"][1]["skipped_checks"])
        self.assertEqual(["generate_chapter", "validate"], session["runs"][0]["workflow_actions"])
        self.assertEqual(["generate_chapter", "validate"], session["runs"][0]["trace_actions"])
        self.assertTrue(session["runs"][0]["trace_plan_aligned"])
        self.assertFalse(session["runs"][1]["trace_plan_aligned"])
        self.assertEqual(1, len(session["recovery_links"]))
        self.assertEqual("chapter_2_test", session["recovery_links"][0]["source_run_id"])
        self.assertIs(session, validate_schema(session, "loop_session.schema.json"))

    def test_loop_session_record_can_capture_failed_stop_error(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session = build_loop_session_record(
            started_at=now,
            finished_at=now,
            requested_steps=2,
            completed_steps=1,
            stopped_reason="failed",
            persist=True,
            stop_on_rejection=True,
            runs=[
                {
                    "run": {
                        "id": "chapter_2_failed",
                        "status": "failed",
                        "committed": False,
                        "chapter_index": 2,
                        "validation": {"problem_codes": ["execution_error"]},
                        "repair_attempts": 0,
                    }
                }
            ],
            error=ValueError("generation failed"),
        )

        self.assertEqual("failed", session["stopped_reason"])
        self.assertEqual(1, session["failed_count"])
        self.assertEqual("ValueError", session["error"]["type"])
        self.assertIs(session, validate_schema(session, "loop_session.schema.json"))

    def test_loop_session_record_rejects_unknown_stopped_reason(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_schema(
                {
                    "id": "loop_bad",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:00+00:00",
                    "requested_steps": 1,
                    "completed_steps": 0,
                    "stopped_reason": "paused",
                    "persist": False,
                    "stop_on_rejection": True,
                    "committed_count": 0,
                    "rejected_count": 0,
                    "failed_count": 0,
                    "first_chapter_index": None,
                    "last_chapter_index": None,
                    "last_run_id": None,
                    "recovery_links": [],
                    "runs": [],
                },
                "loop_session.schema.json",
            )

    def test_minimal_failed_run_result_envelope_is_not_a_valid_run_result(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_run_result({"run": {}})

    def test_failed_run_record_matches_schema(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_failed_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 2,
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 1,
                "notes": [],
            },
            workflow=["generate_chapter", "validate"],
            input_pack="input",
            chapter="",
            validation=None,
            repair_attempts=0,
            workflow_trace=[
                {
                    "action": "generate_chapter",
                    "status": "failed",
                    "started_at": now.isoformat(),
                    "finished_at": now.isoformat(),
                    "chapter_chars": 0,
                    "repair_attempts": 0,
                    "error_type": "ModelOutputError",
                    "error_message": "chapter output is empty",
                }
            ],
            error=ValueError("chapter output is empty"),
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual("unknown", record["director"]["mode"])
        self.assertIsNone(record["workflow_plan"])
        self.assertFalse(record["state_update"]["applied"])
        self.assertIn("execution_error", record["validation"]["problem_codes"])
        self.assertIs(record, validate_schema(record, "run_record.schema.json"))

    def test_director_failed_run_record_matches_schema(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_director_failed_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            director_trace={
                "mode": "model",
                "source": "core.director.model_director.ModelDirector",
                "model": "gpt-test",
                "status": "failed",
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "duration_ms": 0,
                "error_type": "DirectorDecisionError",
                "error_message": "Director model output must be valid JSON",
            },
            error=ValueError("Director model output must be valid JSON"),
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual("failed", record["director"]["status"])
        self.assertEqual([], record["workflow"])
        self.assertIsNone(record["workflow_plan"])
        self.assertFalse(record["state_update"]["applied"])
        self.assertEqual(["director_error"], record["validation"]["problem_codes"])
        self.assertIs(record, validate_schema(record, "run_record.schema.json"))

    def test_workflow_failed_run_record_matches_schema(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = build_workflow_failed_run_record(
            started_at=now,
            finished_at=now,
            base_snapshot={"chapter_index": 2},
            runtime_snapshot={"chapter_index": 2},
            memory_context={"source": "test", "status": "ready", "items": []},
            decision={
                "chapter_index": 2,
                "goal": "bad_workflow",
                "actions": ["generate_chapter", "repair_if_needed", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
                "notes": [],
            },
            director_trace={
                "mode": "model",
                "source": "core.director.model_director.ModelDirector",
                "model": "gpt-test",
                "status": "completed",
                "started_at": now.isoformat(),
                "finished_at": now.isoformat(),
                "duration_ms": 0,
            },
            error=ValueError("repair_if_needed requires validate before it"),
        )

        self.assertEqual("failed", record["status"])
        self.assertEqual([], record["workflow"])
        self.assertIsNone(record["workflow_plan"])
        self.assertFalse(record["state_update"]["applied"])
        self.assertEqual(["workflow_error"], record["validation"]["problem_codes"])
        self.assertEqual(["generate_chapter", "repair_if_needed", "validate"], record["decision"]["actions"])
        self.assertIs(record, validate_schema(record, "run_record.schema.json"))


if __name__ == "__main__":
    unittest.main()
