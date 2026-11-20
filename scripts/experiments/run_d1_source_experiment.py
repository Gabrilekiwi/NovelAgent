"""Reproducible D1 probe using isolated files and existing public context APIs.

Run from the repository with its Python environment. No Provider is constructed.
The synthetic activation below certifies only the harness's parser path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_fact_candidate import normalize_fact_proposal
from core.engine.chapter_fact_context import build_chapter_fact_context
from core.engine.story_project_context import StoryProjectContextService
from core.state.builder import build_snapshot_state_with_audit
from core.state.input_pack import build_input_pack
from core.runtime_paths import RuntimePaths
from core.story_project.activation import activate_story_state, build_story_state_calibration_report
from core.story_project.identity import ensure_project_identity, load_project_identity
from core.story_project.paths import canonical_outline_path
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from scripts.experiments.d1_reviewed_state import (
    RELATIVE_PATH, SourceRuleError, apply_prepared, baseline_request, capture_sources,
    create_baseline, describe_source_conflict, digest, prepare_chapter, read_current,
    resolve_source_conflict, review_request, load_runtime_view,
)


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assert_isolated(root: Path) -> None:
    if not root.resolve().is_relative_to((REPO / ".tmp/stage-b").resolve()):
        raise ValueError("D1 harness accepts only .tmp/stage-b isolated paths")


def probe(root: Path) -> dict:
    assert_isolated(root)
    identity = load_project_identity(root)
    value = read_current(root=root, document=(root / RELATIVE_PATH).read_bytes(),
                         book_id=identity.book_id, next_chapter=3)
    trusted = ChapterCandidateContextBuilder(root, reviewed_state_loader=load_runtime_view).build(3)
    poisoned_snapshot = {**trusted.snapshot, "book_id": identity.book_id,
        "authoritative_state": {"inventory": {"cache_poison": {"quantity": 999}}},
        "characters": {"cache_poison": {"current_goal": "CACHE_STALE_GOAL"}}}
    snapshot, memory = StoryProjectContextService.apply_context(
        trusted.story_project_context, poisoned_snapshot,
        {"source": "forged_cache", "status": "ready", "items": [
            {"type": "story_state", "name": "stale", "data": {"current_location": "CACHE_STALE_LOCATION"}}]},
        snapshot_path=RuntimePaths.for_story_project(root).snapshot_path, allow_legacy_snapshot_adoption=False)
    built = build_snapshot_state_with_audit(snapshot, memory)
    snapshot = StoryProjectContextService.apply_authority(trusted.story_project_context, built["snapshot"])
    input_pack = build_input_pack(snapshot, memory_context=memory, story_project_context=trusted.story_project_context)
    assert "CACHE_STALE" not in input_pack
    view = build_chapter_fact_context(trusted, "林澈与周岚检查旧城站守卫队、信标和警戒等级。").to_dict()
    return {"project_current_state": value["current_state"], "source_decisions": value["source_decisions"],
            "input_pack": input_pack, "memory_context": memory,
            "production_snapshot": snapshot, "production_fact_view": view,
            "production_generation_reads_seven": snapshot.get("authoritative_state") == value["current_state"],
            "production_fact_entity_kinds": sorted({entry["kind"] for entry in view["entity_directory"]}),
            "runtime_cache_policy": trusted.story_project_context["memory_v2"].get("cache_status")}


def cold_probe(root: Path) -> dict:
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--probe", str(root)],
                            cwd=REPO, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def prepare_isolated_book() -> dict:
    directory = REPO / ".tmp/stage-b/d1-attempt-1" / uuid.uuid4().hex
    root = directory / "book"
    assert_isolated(root)
    shutil.copytree(REPO / "tests/fixtures/story_project_semantics/cases/synthetic_standard/book", root)
    outline_two = canonical_outline_path(root, 2)
    outline = outline_two.read_text(encoding="utf-8").replace("结尾压力：", "结尾压力：危险迫使两人立刻做出选择；", 1)
    outline_two.write_text(outline, encoding="utf-8")
    canonical_outline_path(root, 3).write_text(outline.replace("第002章", "第003章"), encoding="utf-8")
    identity = ensure_project_identity(root, book_id="synthetic-d1-" + directory.name)
    report = build_story_state_calibration_report(
        book_id=identity.book_id, parser_version=SEMANTIC_PARSER_VERSION,
        semantic_schema_version="1.0", target_layout_profile_version="canonical-zh-1",
        evidence={"target_sample_count": 1, "format_variant_count": 2, "managed_round_trip_rate": 1.0,
                  "required_field_exact_match_rate": 1.0, "authoritative_precision": 1.0,
                  "supported_optional_recall": .95, "unsupported_structure_count": 1,
                  "unsupported_structure_captured_count": 1, "consecutive_shadow_chapters": 10,
                  "blocking_conflict_count": 0, "missing_provenance_fields": []})
    activate_story_state(root, report)  # Synthetic fixture qualification, never a real calibration claim.
    state = json.loads((REPO / "tests/fixtures/chapter_fact_offline_v1/baseline.json").read_text(encoding="utf-8"))
    # Exercise a real legacy redundancy, not only clean synthetic ledgers.
    state["characters"]["char_lin_che"]["location"] = "location_warehouse"
    state["characters"]["char_lin_che"]["source_tier"] = "story_project_standard"
    static_path = root / "设定/D1静态约束.md"
    static_text = "D1_STATIC_RULE：信标只有在金属表面才能固定，不能漂浮在空中。"
    static_path.write_text(static_text + "\n", encoding="utf-8")
    static_references = [{"relative_path": "设定/D1静态约束.md", "sha256": digest(static_path.read_bytes()),
                          "text": static_text}]
    example = json.loads((REPO / "examples/chapter_fact_review/seven_kinds.json").read_text(encoding="utf-8"))
    facts = normalize_fact_proposal(example["proposal"], chapter_text=example["chapter_text"])
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    manual = root / "追踪/上下文.md"
    manual_before = manual.read_bytes()
    confirmation = baseline_request(book_id=identity.book_id, chapter=1, state=state, sources=capture_sources(root),
                                     static_references=static_references)
    document = create_baseline(root=root, book_id=identity.book_id, chapter=1, state=state,
                                confirmed_baseline=confirmation, static_references=static_references)
    inputs = dict(root=root, document=document, book_id=identity.book_id, chapter=2,
                  chapter_text=example["chapter_text"], draft_revision="fixture-draft-1", facts=facts)
    inputs.update(review_binding=review_request(**inputs), review_revision="fixture-review-1")
    plan = prepare_chapter(**inputs)
    document = apply_prepared(plan=plan, **inputs)
    (root / RELATIVE_PATH).write_bytes(document)
    (root / "正文/第002章_D1隔离试验.md").write_text(example["chapter_text"], encoding="utf-8")
    assert manual.read_bytes() == manual_before
    return {"directory": directory, "root": root, "identity": identity, "manual": manual,
            "manual_before": manual_before, "document": document, "inputs": inputs, "plan": plan}


def run() -> dict:
    prepared = prepare_isolated_book()
    directory, root, identity, manual, manual_before, document, plan = (
        prepared[key] for key in ("directory", "root", "identity", "manual", "manual_before", "document", "plan"))
    memory = RuntimePaths.for_story_project(root).memory_dir / "v2"
    memory.mkdir(parents=True, exist_ok=True)
    cache = memory / "canonical_memory.json"
    save(cache, {"poisoned_cache": True, "authoritative_state": {"inventory": {"fake": {"quantity": 999}}}})
    poisoned = cold_probe(root)
    save(directory / "with-poisoned-cache.json", poisoned)
    cache.unlink()  # This exact file was created in the unique experiment directory above.
    cold = cold_probe(root)
    save(directory / "without-cache.json", cold)
    assert cold["project_current_state"] == poisoned["project_current_state"] == plan["prepared"]["state_after"]
    assert cold["production_snapshot"] == poisoned["production_snapshot"]
    assert "D1_STATIC_RULE" in cold["input_pack"]
    assert cold["project_current_state"]["characters"]["char_lin_che"]["location"] == "location_old_station"
    manual.write_bytes(manual_before.replace("旧城站封闭闸门外".encode(), "人工指定的码头".encode()))
    try:
        read_current(root=root, document=document, book_id=identity.book_id, next_chapter=3)
    except SourceRuleError as exc:
        assert exc.code == "manual_source_changed"
    else:
        raise AssertionError("manual source conflict was not detected")
    decision = {"conflict": describe_source_conflict(root=root, document=document, book_id=identity.book_id),
                "action": "keep_reviewed_current", "review_source": "author",
                "reason": "合成处理决定：保留章节确认值，人工后改内容保留为原文。"}
    document = resolve_source_conflict(root=root, document=document, book_id=identity.book_id, decision=decision)
    (root / RELATIVE_PATH).write_bytes(document)
    resolved = cold_probe(root)
    save(directory / "resolved-source-conflict.json", resolved)
    assert resolved["source_decisions"] == [decision]
    assert resolved["project_current_state"] == cold["project_current_state"]
    manual.write_bytes(manual.read_bytes().replace("人工指定的码头".encode(), "再次指定的北门".encode()))
    try:
        read_current(root=root, document=document, book_id=identity.book_id, next_chapter=3)
    except SourceRuleError as exc:
        assert exc.code == "manual_source_changed"
    else:
        raise AssertionError("saved conflict decision survived a different source revision")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                            text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True,
                           text=True, check=True).stdout.strip()
    code_paths = ["core/engine/chapter_candidate_context.py", "core/engine/chapter_fact_context.py",
                  "core/engine/chapter_fact_prepare.py", "core/engine/story_project_context.py",
                  "core/state/input_pack.py", "core/state/location_mirrors.py",
                  "core/story_project/reviewed_state_context.py", "core/story_project/reviewed_static_context.py"]
    result = {"directory": directory.relative_to(REPO).as_posix(), "base_commit": commit,
              "working_tree_dirty": bool(dirty),
              "code_files": [{"path": path, "sha256": digest((REPO / path).read_bytes())} for path in code_paths],
              "candidate_source_rules": "passed",
              "current_collections": [name for name in ("characters", "relationships", "roster", "locations",
                  "inventory", "numeric_counters", "events") if name in cold["project_current_state"]],
              "manual_original_preserved_at_chapter_write": True, "cold_processes": 3,
              "persisted_conflict_decision_cold_read": True, "source_reedit_invalidated": True,
              "production_bridge": "pending" if not cold["production_generation_reads_seven"] else "passed_with_injected_source_loader",
              "normal_entrypoints_enabled": False,
              "full_input_path_exercised": True,
              "production_generation_reads_seven": cold["production_generation_reads_seven"],
              "production_fact_entity_kinds": cold["production_fact_entity_kinds"],
              "formal_transaction_exercised": False, "real_strict_qualification_claimed": False,
              "paid_provider_calls": 0, "real_book_writes": 0,
              "script_sha256": digest(Path(__file__).read_bytes()),
              "candidate_sha256": digest((REPO / "scripts/experiments/d1_reviewed_state.py").read_bytes())}
    save(directory / "report.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path)
    args = parser.parse_args()
    print(json.dumps(probe(args.probe) if args.probe else run(), ensure_ascii=True, indent=2))
