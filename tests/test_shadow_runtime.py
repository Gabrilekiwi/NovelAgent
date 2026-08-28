from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
import unittest
import uuid

from core.engine.executor import AgentExecutor
from core.engine.preflight import SCHEMA_ASSETS
from core.engine.run_record import validate_run_result
from core.engine.workflow import build_workflow_plan
from core.runtime import (
    RuntimeContractError,
    build_shadow_runtime_evidence,
    resolve_runtime_mode,
    validate_runtime_evidence,
)


class ShadowRuntimeContractTest(unittest.TestCase):
    def _plan(self) -> dict:
        return build_workflow_plan(
            {
                "goal": "continue_existing_arc",
                "actions": ["generate_chapter", "validate"],
                "validation_focus": ["logic"],
                "max_repair_attempts": 0,
            }
        )

    def _trace(self, *, validation_status: str = "completed") -> list[dict]:
        return [
            {
                "action": "generate_chapter",
                "status": "completed",
                "started_at": "2026-08-28T00:00:00+00:00",
                "finished_at": "2026-08-28T00:00:01+00:00",
                "duration_ms": 1000,
                "chapter_chars": 120,
                "repair_attempts": 0,
            },
            {
                "action": "validate",
                "status": validation_status,
                "started_at": "2026-08-28T00:00:01+00:00",
                "finished_at": "2026-08-28T00:00:02+00:00",
                "duration_ms": 1000,
                "chapter_chars": 120,
                "repair_attempts": 0,
            },
        ]

    def _run(self, *, status: str = "preview", validation_status: str = "completed") -> dict:
        return {
            "id": "chapter_2_runtime_test",
            "status": status,
            "started_at": "2026-08-28T00:00:00+00:00",
            "finished_at": "2026-08-28T00:00:02+00:00",
            "workflow_plan": self._plan(),
            "trace": self._trace(validation_status=validation_status),
        }

    def test_mode_resolver_keeps_node_cutover_unavailable(self) -> None:
        self.assertEqual("legacy", resolve_runtime_mode(None))
        self.assertEqual("shadow", resolve_runtime_mode(" SHADOW "))
        with self.assertRaisesRegex(RuntimeContractError, "future execution cutover"):
            resolve_runtime_mode("node")
        with self.assertRaises(RuntimeContractError):
            resolve_runtime_mode("experimental")

    def test_runtime_schema_is_covered_by_preflight(self) -> None:
        self.assertIn(Path("schemas/runtime_evidence.schema.json"), SCHEMA_ASSETS)

    def test_shadow_projection_records_contracts_without_raw_context(self) -> None:
        evidence = build_shadow_runtime_evidence(
            self._run(),
            context_inputs={
                "snapshot": {"chapter_index": 2},
                "input_pack": "PRIVATE PROMPT CONTENT",
                "workflow_plan": self._plan(),
                "chapter": "Generated chapter.",
                "validation": {"ok": True},
                "analysis": {"summary": "done"},
            },
            model_call_receipts=[
                {
                    "stage": "chapter_generation",
                    "attempt_id": "attempt-1",
                    "received_at": "2026-08-28T00:00:01+00:00",
                    "receipt_hash": "a" * 64,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                {
                    "stage": "claude_polish",
                    "attempt_id": "attempt-2",
                    "received_at": "2026-08-28T00:00:02+00:00",
                    "receipt_hash": "b" * 64,
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "output_tokens": 5,
                    },
                }
            ],
            created_at="2026-08-28T00:00:02+00:00",
        )

        self.assertEqual("shadow", evidence["mode"])
        self.assertEqual("legacy_executor", evidence["authority"])
        self.assertFalse(evidence["execution_cutover"])
        self.assertEqual(2, len(evidence["workflow_snapshot"]["nodes"]))
        self.assertEqual(2, len(evidence["attempts"]))
        self.assertEqual(2, len(evidence["checkpoints"]))
        self.assertTrue(all(not item["resume_capable"] for item in evidence["checkpoints"]))
        self.assertEqual("not_needed", evidence["recovery"]["decision"])
        usage_by_node = {item["node_id"]: item for item in evidence["usage"]}
        self.assertEqual(
            {
                "node_id": "generate_chapter",
                "source": "model_call_receipts",
                "receipt_hashes": ["a" * 64],
                "call_count": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "complete": True,
            },
            usage_by_node["generate_chapter"],
        )
        self.assertEqual(15, usage_by_node["execution_support"]["input_tokens"])
        self.assertEqual(5, usage_by_node["execution_support"]["output_tokens"])
        self.assertEqual(20, usage_by_node["execution_support"]["total_tokens"])
        self.assertTrue(usage_by_node["execution_support"]["complete"])
        self.assertNotIn("PRIVATE PROMPT CONTENT", json.dumps(evidence))
        self.assertEqual(evidence, validate_runtime_evidence(evidence))

    def test_failed_node_is_only_a_non_authoritative_recovery_hint(self) -> None:
        evidence = build_shadow_runtime_evidence(
            self._run(status="failed", validation_status="failed"),
            created_at="2026-08-28T00:00:02+00:00",
        )

        self.assertEqual("legacy_executor_required", evidence["recovery"]["decision"])
        self.assertEqual("validate", evidence["recovery"]["resume_node_id"])
        self.assertFalse(evidence["recovery"]["authoritative"])
        self.assertFalse(evidence["checkpoints"][-1]["resume_capable"])

    def test_runtime_evidence_rejects_hash_tampering(self) -> None:
        evidence = build_shadow_runtime_evidence(
            self._run(),
            created_at="2026-08-28T00:00:02+00:00",
        )
        tampered = copy.deepcopy(evidence)
        tampered["recovery"]["reason"] = "tampered"

        with self.assertRaisesRegex(RuntimeContractError, "hash mismatch"):
            validate_runtime_evidence(tampered)

    def test_runtime_evidence_rejects_checkpoint_attempt_drift(self) -> None:
        evidence = build_shadow_runtime_evidence(
            self._run(),
            created_at="2026-08-28T00:00:02+00:00",
        )
        tampered = copy.deepcopy(evidence)
        tampered["checkpoints"][0]["trace_digest"] = "f" * 64

        with self.assertRaisesRegex(RuntimeContractError, "does not match attempt"):
            validate_runtime_evidence(tampered)

    def test_runtime_evidence_rejects_usage_for_unknown_node(self) -> None:
        evidence = build_shadow_runtime_evidence(
            self._run(),
            model_call_receipts=[
                {
                    "stage": "chapter_generation",
                    "attempt_id": "attempt-1",
                    "received_at": "2026-08-28T00:00:01+00:00",
                    "receipt_hash": "a" * 64,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ],
            created_at="2026-08-28T00:00:02+00:00",
        )
        tampered = copy.deepcopy(evidence)
        tampered["usage"][0]["node_id"] = "unknown_node"

        with self.assertRaisesRegex(RuntimeContractError, "unknown node"):
            validate_runtime_evidence(tampered)


class ShadowRuntimeExecutorCompatibilityTest(unittest.TestCase):
    def _case_dir(self, name: str) -> Path:
        path = Path.cwd() / ".tmp" / "test_shadow_runtime" / f"{name}_{uuid.uuid4().hex}"
        path.mkdir(parents=True)
        return path

    def _executor(self, root: Path, *, runtime_mode: str, persistable: bool) -> AgentExecutor:
        snapshot_path = root / "snapshot.json"
        snapshot_path.write_text(
            Path("data/snapshot.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return AgentExecutor(
            snapshot_path=snapshot_path,
            memory_path=Path("data/notion_memory.example.json"),
            run_dir=root / "runs",
            chapter_dir=root / "chapters",
            persistence_dir=root / "transactions",
            dry_run=True,
            use_run_history=False,
            enable_execution_provenance=persistable,
            runtime_mode=runtime_mode,
        )

    def _without_run_identity(self, value):
        if isinstance(value, dict):
            return {
                key: self._without_run_identity(child)
                for key, child in value.items()
                if key != "source_run_id"
            }
        if isinstance(value, list):
            return [self._without_run_identity(child) for child in value]
        return value

    def test_shadow_mode_does_not_change_legacy_execution_outputs(self) -> None:
        legacy = self._executor(
            self._case_dir("legacy"), runtime_mode="legacy", persistable=False
        ).run_once(persist=False)
        shadow = self._executor(
            self._case_dir("shadow"), runtime_mode="shadow", persistable=False
        ).run_once(persist=False)

        for key in (
            "chapter",
            "decision",
            "workflow",
            "workflow_plan",
            "validation",
            "analysis",
            "repair_attempts",
            "accepted",
            "committed",
        ):
            self.assertEqual(legacy[key], shadow[key], key)
        self.assertEqual(
            self._without_run_identity(legacy["snapshot"]),
            self._without_run_identity(shadow["snapshot"]),
        )
        self.assertNotIn("runtime", legacy["run"])
        self.assertEqual("shadow", shadow["run"]["runtime"]["mode"])
        self.assertIsNone(shadow["run"]["runtime"]["artifact_ref"])
        self.assertEqual(shadow, validate_run_result(shadow))

    def test_persisted_shadow_run_writes_execution_scoped_evidence(self) -> None:
        root = self._case_dir("persisted")
        result = self._executor(
            root, runtime_mode="shadow", persistable=True
        ).run_once(persist=True)

        runtime = result["run"]["runtime"]
        artifact_ref = runtime["artifact_ref"]
        self.assertIsInstance(artifact_ref, str)
        artifact_path = (root / "runs").joinpath(*PurePosixPath(artifact_ref).parts)
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(runtime, json.loads(artifact_path.read_text(encoding="utf-8")))
        persisted_run = json.loads(
            ((root / "runs") / f"{result['run']['id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(runtime["evidence_hash"], persisted_run["run"]["runtime"]["evidence_hash"])


if __name__ == "__main__":
    unittest.main()
