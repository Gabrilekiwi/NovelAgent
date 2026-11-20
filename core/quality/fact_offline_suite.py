"""Run versioned local cases without a Provider or production state access."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from core.engine.chapter_fact_candidate import ChapterFactCandidateError, normalize_fact_proposal
from core.engine.chapter_fact_prepare import ChapterFactPreparationError, prepare_confirmed_fact_changes
from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Offline fixture path escapes its allowed root")
    return path


def _load_checked(root: Path, relative: str, expected_hash: str):
    path = _within(root, relative)
    if _hash(path) != expected_hash:
        raise ValueError(f"Offline fixture hash mismatch: {relative}")
    return json.loads(path.read_text(encoding="utf-8"))


def run_fact_offline_suite(suite_dir: Path, *, repository: Path | None = None,
                           verify_local_sources: bool = False) -> dict:
    manifest_path = suite_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0":
        raise ValueError("Unsupported offline suite schema version")
    baseline = _load_checked(suite_dir, manifest["baseline_path"], manifest["baseline_sha256"])
    seen = set()
    results = []
    for item in manifest["cases"]:
        if item["id"] in seen:
            raise ValueError("Duplicate offline case id")
        seen.add(item["id"])
        case = _load_checked(suite_dir, item["path"], item["sha256"])
        if case["id"] != item["id"]:
            raise ValueError("Offline case id differs from manifest")
        material = case["material"]
        if case["checker"] == "fact_prepare":
            if material["baseline"] != manifest["baseline_path"]:
                raise ValueError("Case must use the suite's verified baseline")
            base = copy.deepcopy(baseline)
            for collection, key in material.get("baseline_omissions", []):
                del base[collection][key]
            for collection, additions in material.get("baseline_additions", {}).items():
                base[collection].update(copy.deepcopy(additions))
            try:
                facts = normalize_fact_proposal(material["proposal"], chapter_text=material["chapter_text"])
                for fact in facts:
                    # These are explicit test decisions, not a record of a user
                    # accepting any real manuscript or any policy auto-approval.
                    fact["review"]["decision"] = material.get("review_decisions", {}).get(fact["local_key"], "accepted")
                plan = prepare_confirmed_fact_changes(facts=facts, chapter_text=material["chapter_text"], base_state=base)
                observed = {"disposition": "accept", "codes": [], "computed_changes": len(plan["computed_changes"])}
            except (ChapterFactCandidateError, ChapterFactPreparationError) as exc:
                observed = {"disposition": "block", "codes": [exc.code]}
        elif case["checker"] == "integrity":
            report = FinalArtifactIntegrityGate().evaluate(artifact_text=material["chapter_text"],
                                                          stage=material["stage"], scene_events=material.get("scene_events", []))
            observed = {"disposition": "accept" if report["accepted"] else "block",
                        "codes": sorted({f["code"] for f in report["findings"]})}
        else:
            raise ValueError(f"Unsupported offline checker {case['checker']!r}")
        expected = case["expected"]
        if expected["disposition"] == "review":
            # No semantic policy is plugged in yet. A structurally accepted
            # case must stay a visible coverage gap, never a green quality score.
            outcome = "policy_gap"
        else:
            matched = observed["disposition"] == expected["disposition"]
            if expected.get("code"):
                matched = matched and expected["code"] in observed["codes"]
            outcome = "verified" if matched else "regression"
        results.append({"id": case["id"], "category": case["category"], "expected": expected,
                        "observed": observed, "outcome": outcome, "annotation": case["annotation"]})
    local_sources = []
    if verify_local_sources:
        if repository is None:
            raise ValueError("Repository is required to verify explicitly selected local sources")
        for source in manifest["external_readonly_sources"]:
            original = _within(repository, source["path"])
            snapshot = _within(repository, source["snapshot"])
            local_sources.append({"id": source["id"], "source_matches": original.is_file() and _hash(original) == source["sha256"],
                                  "snapshot_matches": snapshot.is_file() and _hash(snapshot) == source["snapshot_sha256"],
                                  "scored": False})
    return {"suite_id": manifest["suite_id"], "manifest_sha256": _hash(manifest_path),
            "case_count": len(results), "verified": sum(r["outcome"] == "verified" for r in results),
            "regressions": sum(r["outcome"] == "regression" for r in results),
            "policy_gaps": sum(r["outcome"] == "policy_gap" for r in results), "results": results,
            "local_sources": local_sources, "paid_provider_calls": 0,
            "human_quality_scored": False, "model_effectiveness_evaluated": False}
