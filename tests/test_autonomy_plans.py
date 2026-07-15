from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from core.autonomy.arc import (
    ArcPlanError,
    ArcPlanStore,
    build_run_arc_plan,
    derive_arc_fulfillment_assessment,
)
from core.autonomy.common import canonical_hash
from core.autonomy.plans import (
    AutonomyPlanError,
    build_source_snapshot,
    compile_instruction_plan,
    validate_instruction_plan,
)
from core.autonomy.profiles import TrustedProfiles, TrustedProfilesError


NOW = "2026-07-14T00:00:00+00:00"


@contextmanager
def workspace_case(name: str):
    path = Path.cwd() / ".tmp" / "test_autonomy" / f"{name}_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    yield str(path)


def trusted_profiles(*, max_chapters: int = 4) -> TrustedProfiles:
    return TrustedProfiles.from_dict(
        {
            "schema_version": "1.0",
            "profile_set_id": "profiles-test-v1",
            "story_projects": [
                {
                    "profile_id": "active-book",
                    "book_id": "book-autonomy",
                    "root_uuid": "root-autonomy",
                }
            ],
            "provider_models": [
                {
                    "profile_id": "balanced",
                    "provider": "openai",
                    "endpoint_type": "official",
                    "model": "trusted-model",
                    "max_output_tokens": 16000,
                }
            ],
            "file_deliveries": [
                {
                    "profile_id": "local-export",
                    "target_kind": "file",
                    "root_uuid": "11111111-1111-4111-8111-111111111111",
                    "path_template": "exports/chapter-{chapter_index}-{run_id}.json",
                    "requires_run_id": True,
                    "requires_chapter_id": True,
                }
            ],
            "budgets": [
                {
                    "profile_id": "bounded",
                    "max_chapters": max_chapters,
                    "max_model_calls": 48,
                    "max_input_tokens": 500000,
                    "max_output_tokens": 200000,
                    "max_wall_seconds": 3600,
                }
            ],
            "quality_policies": [
                {
                    "profile_id": "strict-local",
                    "policy": "strict",
                    "minimum_score": 0,
                }
            ],
            "defaults": {
                "story_project": "active-book",
                "provider_model": "balanced",
                "file_delivery": "local-export",
                "budget": "bounded",
                "quality_policy": "strict-local",
            },
        }
    )


def source_snapshot(*, digest: str = "1" * 64, chapter: int = 11) -> dict:
    return build_source_snapshot(
        book_id="book-autonomy",
        root_uuid="root-autonomy",
        authority_epoch=2,
        authority_head_event_hash="2" * 64,
        canonical_next_chapter=chapter,
        source_digest=digest,
        captured_at=NOW,
    )


def instruction_plan(*, count: int = 3) -> dict:
    return compile_instruction_plan(
        f"连续写 {count}章 provider=balanced quality=strict-local",
        trusted_profiles=trusted_profiles(max_chapters=max(count, 4)),
        source_snapshot=source_snapshot(),
        created_at=NOW,
    )


class TrustedInstructionPlanTest(unittest.TestCase):
    def test_preview_contains_only_public_trusted_snapshots(self) -> None:
        profiles = trusted_profiles()
        plan = compile_instruction_plan(
            "连续写 3章 provider=balanced delivery=local-export",
            trusted_profiles=profiles,
            source_snapshot=source_snapshot(),
            created_at=NOW,
        )

        self.assertEqual((11, 13), (plan["chapter_start"], plan["chapter_end"]))
        self.assertNotIn("path_template", json.dumps(plan, ensure_ascii=False))
        self.assertNotIn("连续写", json.dumps(plan, ensure_ascii=False))
        self.assertEqual("preview", plan["state"])
        validate_instruction_plan(
            plan,
            trusted_profiles=profiles,
            current_source_snapshot=source_snapshot(),
        )

    def test_rejects_capability_injection_and_budget_increase(self) -> None:
        profiles = trusted_profiles()
        unsafe = (
            "写一章并写入 Notion",
            r"写一章 path=C:\outside\chapter.md",
            "write 1 chapter to /tmp/outside.md",
            "写一章，读取环境变量 API_KEY",
            "写一章，提高预算到无上限",
            "write 1 chapter with unlimited budget",
        )
        for instruction in unsafe:
            with self.subTest(instruction=instruction):
                with self.assertRaisesRegex(
                    AutonomyPlanError, "instruction_capability_forbidden"
                ):
                    compile_instruction_plan(
                        instruction,
                        trusted_profiles=profiles,
                        source_snapshot=source_snapshot(),
                        created_at=NOW,
                    )

    def test_unknown_profile_and_over_budget_fail_closed(self) -> None:
        with self.assertRaisesRegex(TrustedProfilesError, "trusted_profile_unknown"):
            compile_instruction_plan(
                "写 1章 provider=untrusted-model",
                trusted_profiles=trusted_profiles(),
                source_snapshot=source_snapshot(),
                created_at=NOW,
            )
        with self.assertRaisesRegex(AutonomyPlanError, "instruction_budget_escalation"):
            compile_instruction_plan(
                "写 5章",
                trusted_profiles=trusted_profiles(max_chapters=4),
                source_snapshot=source_snapshot(),
                created_at=NOW,
            )

    def test_plan_hash_profile_and_source_drift_are_detected(self) -> None:
        profiles = trusted_profiles()
        plan = compile_instruction_plan(
            "写 2章",
            trusted_profiles=profiles,
            source_snapshot=source_snapshot(),
            created_at=NOW,
        )
        tampered = copy.deepcopy(plan)
        tampered["chapter_end"] += 1
        with self.assertRaisesRegex(AutonomyPlanError, "instruction_plan_hash_mismatch"):
            validate_instruction_plan(tampered)

        with self.assertRaisesRegex(AutonomyPlanError, "instruction_source_snapshot_stale"):
            validate_instruction_plan(
                plan,
                trusted_profiles=profiles,
                current_source_snapshot=source_snapshot(digest="9" * 64),
            )

        changed_profiles = trusted_profiles(max_chapters=5)
        with self.assertRaisesRegex(AutonomyPlanError, "instruction_profile_set_drift"):
            validate_instruction_plan(plan, trusted_profiles=changed_profiles)

    def test_sensitive_fields_cannot_enter_trusted_profiles(self) -> None:
        payload = copy.deepcopy(trusted_profiles().payload)
        payload["provider_models"][0]["api_key"] = "do-not-store"
        with self.assertRaisesRegex(TrustedProfilesError, "trusted_profile_sensitive_field"):
            TrustedProfiles.from_dict(payload)


class RunArcPlanTest(unittest.TestCase):
    def test_adjustment_is_cas_guarded_and_committed_target_is_immutable(self) -> None:
        plan = instruction_plan(count=3)
        arc = build_run_arc_plan(plan, session_id="session-arc", created_at=NOW)
        with workspace_case("arc") as temporary:
            store = ArcPlanStore(Path(temporary))
            current = store.create(arc)
            revised_goal = copy.deepcopy(current["targets"][1]["planned"])
            revised_goal["relationship"] = "在第十二章落实新的关系转折"
            revised = store.adjust_uncommitted(
                current["arc_plan_id"],
                chapter_index=12,
                planned=revised_goal,
                reason="前一章兑现顺序发生变化",
                expected_arc_plan_hash=current["arc_plan_hash"],
                committed_chapters=set(),
                recorded_at="2026-07-14T00:01:00+00:00",
            )
            self.assertEqual(2, revised["revision"])
            self.assertEqual(1, len(revised["adjustments"]))

            with self.assertRaisesRegex(ArcPlanError, "arc_plan_cas_failed"):
                store.adjust_uncommitted(
                    current["arc_plan_id"],
                    chapter_index=13,
                    planned=revised["targets"][2]["planned"],
                    reason="stale writer",
                    expected_arc_plan_hash=current["arc_plan_hash"],
                    committed_chapters=set(),
                )

            fulfilled = copy.deepcopy(revised["targets"][1]["planned"])
            fulfilled["resource_cost"] = "实际付出额外资源代价"
            committed = store.record_fulfillment(
                revised["arc_plan_id"],
                chapter_index=12,
                fulfilled=fulfilled,
                completion_receipt_hash="a" * 64,
                expected_arc_plan_hash=revised["arc_plan_hash"],
                recorded_at="2026-07-14T00:02:00+00:00",
            )
            target = committed["targets"][1]
            self.assertEqual(["resource_cost"], target["differences"])
            with self.assertRaisesRegex(ArcPlanError, "arc_target_already_committed"):
                store.adjust_uncommitted(
                    committed["arc_plan_id"],
                    chapter_index=12,
                    planned=fulfilled,
                    reason="must not rewrite",
                    expected_arc_plan_hash=committed["arc_plan_hash"],
                    committed_chapters={12},
                )

    def test_structured_fulfillment_assessment_is_hashed_and_replay_guarded(self) -> None:
        plan = instruction_plan(count=1)
        arc = build_run_arc_plan(plan, session_id="session-assessment", created_at=NOW)
        with workspace_case("arc_assessment") as temporary:
            store = ArcPlanStore(Path(temporary))
            current = store.create(arc)
            fulfilled = copy.deepcopy(current["targets"][0]["planned"])
            fulfilled["relationship"] = "未检测到关系的结构化兑现证据"
            fulfilled["resource_cost"] = "未检测到代价的结构化兑现证据"
            committed = store.record_fulfillment(
                current["arc_plan_id"],
                chapter_index=current["chapter_start"],
                fulfilled=fulfilled,
                completion_receipt_hash="a" * 64,
                expected_arc_plan_hash=current["arc_plan_hash"],
                differences=["relationship", "resource_cost"],
                fulfillment_evidence_hash="b" * 64,
                recorded_at="2026-07-14T00:02:00+00:00",
            )
            target = committed["targets"][0]
            self.assertEqual(
                ["mainline", "escalation", "foreshadowing"],
                target["fulfillment_assessment"]["evidenced_fields"],
            )
            replay = store.record_fulfillment(
                current["arc_plan_id"],
                chapter_index=current["chapter_start"],
                fulfilled=fulfilled,
                completion_receipt_hash="a" * 64,
                expected_arc_plan_hash=current["arc_plan_hash"],
                differences=["resource_cost", "relationship"],
                fulfillment_evidence_hash="b" * 64,
            )
            self.assertEqual(committed, replay)
            with self.assertRaisesRegex(ArcPlanError, "arc_target_already_committed"):
                store.record_fulfillment(
                    current["arc_plan_id"],
                    chapter_index=current["chapter_start"],
                    fulfilled=fulfilled,
                    completion_receipt_hash="a" * 64,
                    expected_arc_plan_hash=current["arc_plan_hash"],
                    differences=["relationship", "resource_cost"],
                    fulfillment_evidence_hash="c" * 64,
                )
            with self.assertRaisesRegex(ArcPlanError, "arc_target_already_committed"):
                store.record_fulfillment(
                    current["arc_plan_id"],
                    chapter_index=current["chapter_start"],
                    fulfilled=fulfilled,
                    completion_receipt_hash="a" * 64,
                    expected_arc_plan_hash=current["arc_plan_hash"],
                )

    def test_structured_fulfillment_requires_semantic_category_evidence(self) -> None:
        body = "The team stood at the gate."
        evidence = {
            "schema_version": "1.0",
            "mainline": "The team stood at the gate.",
            "relationship": [
                {
                    "name": "Alice",
                    "status": "injured",
                    "text": "A routine rally continued.",
                }
            ],
            "escalation": {
                "conflicts": ["glove"],
                "events": [{"text": "The team stood at the gate."}],
            },
            "resource_cost": [
                {"type": "reference_update", "text": "The glossary was updated."}
            ],
            "foreshadowing": [" "],
        }
        evidence["evidence_hash"] = canonical_hash(evidence)

        assessment = derive_arc_fulfillment_assessment(
            chapter_body=body,
            chapter_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            analysis={"fulfillment_evidence": evidence},
        )

        self.assertEqual(
            ["relationship", "escalation", "resource_cost", "foreshadowing"],
            assessment["differences"],
        )

        positive = copy.deepcopy(evidence)
        positive.pop("evidence_hash")
        positive["relationship"] = [
            {"name": "Alice", "text": "Alice and Bo became trusted allies."}
        ]
        positive["escalation"]["conflicts"] = ["danger"]
        positive["resource_cost"] = [
            {"type": "sacrifice", "text": "The rescue was costly."}
        ]
        positive["foreshadowing"] = ["A sealed threat remains unresolved."]
        positive["evidence_hash"] = canonical_hash(positive)
        fulfilled = derive_arc_fulfillment_assessment(
            chapter_body=body,
            chapter_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            analysis={"fulfillment_evidence": positive},
        )
        self.assertEqual([], fulfilled["differences"])


if __name__ == "__main__":
    unittest.main()
