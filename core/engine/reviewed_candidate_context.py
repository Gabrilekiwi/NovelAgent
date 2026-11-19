"""Explicit internal v3 Candidate reader; ordinary writer entrypoints stay closed.

Uses the real upgraded identity and receipt chain, never a downgraded identity
or patched legacy loader. It is read-only and does not reconcile or grant a
publication permit. Services must own their writer/pending-transaction boundary.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

from core.engine.chapter_candidate_context import (
    CHAPTER_CANDIDATE_CONTEXT_VERSION, ChapterCandidateContextBuilder, ChapterCandidateContextError,
    TrustedChapterCandidateContext, _read_set_source_binding,
)
from core.memory_v2.canonical import canonical_json_hash
from core.runtime_paths import RuntimePaths
from core.state.memory import normalize_memory_context
from core.state.snapshot import normalize_snapshot
from core.story_project.activation import evaluate_story_state_activation
from core.story_project.mapper import build_story_project_runtime_context
from core.story_project.read_set import StoryProjectSourceDriftError, capture_story_project_read_set, _verify_story_project_read_set
from core.story_project.reviewed_identity import (
    REVIEWED_WRITER_CONTRACT, load_verified_reviewed_identity, validate_reviewed_identity,
)
from core.story_project.reviewed_state_context import build_reviewed_state_context, validated_reviewed_state_context
from core.story_project.reviewed_state_document import RELATIVE_PATH, ReviewedStateDocumentError, verify_current_sources
from core.story_project.paths import infer_next_chapter
from core.story_project.runtime import RUNTIME_CACHE_POLICY_BYPASS
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION, parse_story_project_semantic_state
from core.story_project.validator import validate_story_project


def _current(root: Path, *, chapter: int):
    verified = load_verified_reviewed_identity(root)
    identity = validate_reviewed_identity(verified["identity"])
    if identity.authority["minimum_writer_contract"] > REVIEWED_WRITER_CONTRACT:
        raise ChapterCandidateContextError("reviewed identity requires a newer supported contract")
    history = verified["history"]
    try:
        verify_current_sources(root=root, value=history["value"], next_chapter=chapter)
    except ReviewedStateDocumentError as exc:
        # These are known changes to a previously valid source, not corrupt
        # Candidate evidence. Keep all other integrity failures distinct.
        if exc.code not in {"reviewed_manual_source_changed", "reviewed_static_reference_changed",
                            "reviewed_next_chapter_mismatch"}:
            raise
        raise StoryProjectSourceDriftError("prepare", [{"field": "reviewed_sources", "code": exc.code}]) from exc
    if infer_next_chapter(root) != chapter:
        raise ChapterCandidateContextError("reviewed state and prose cursor differ")
    return identity, history


def verify_reviewed_candidate_read_set(read_set: dict, *, phase: str) -> dict:
    """Read-only phases only; never allow declared writes through this reader."""
    if phase not in {"prepare", "pre_apply"}:
        raise ChapterCandidateContextError("reviewed Candidate reader cannot verify a write transition")
    root = Path(read_set["root_identity"]["resolved_path"])
    identity, _ = _current(root, chapter=read_set["chapter_index"])
    return _verify_story_project_read_set(read_set, phase=phase, source_identity=identity)


def validate_preparation_identity(context: dict):
    """Identity adapter only for the explicitly reviewed preparation workflow."""
    from core.story_project.identity import validate_project_identity
    payload = context["project_identity"]
    if payload.get("schema_version") != "3.0":
        return validate_project_identity(payload)
    identity, history = _current(Path(context["story_project_root"]), chapter=context["chapter_index"])
    view = validated_reviewed_state_context(context)
    if (identity.to_dict() != payload or view is None
            or view["source_sha256"] != history["document_sha256"]
            or view["authoritative_state"] != history["value"]["current_state"]):
        raise ChapterCandidateContextError("prepared reviewed identity or state differs from verified history")
    return identity


def verify_preparation_sources(context: dict, *, phase: str):
    from core.story_project.read_set import verify_story_project_read_set
    if context["project_identity"].get("schema_version") == "3.0":
        validate_preparation_identity(context)
        return verify_reviewed_candidate_read_set(context["read_set"], phase=phase)
    return verify_story_project_read_set(context["read_set"], phase=phase)


class ReviewedChapterCandidateContextBuilder(ChapterCandidateContextBuilder):
    def __init__(self, story_project_root):
        super().__init__(story_project_root)

    def _load_persisted_identity(self):
        identity = validate_reviewed_identity(load_verified_reviewed_identity(self.story_project_root)["identity"])
        if identity.authority["minimum_writer_contract"] > REVIEWED_WRITER_CONTRACT:
            raise ChapterCandidateContextError("reviewed identity requires a newer supported contract")
        return identity

    def validate_identity(self, payload):
        return validate_reviewed_identity(payload)

    def _verify_read_set(self, read_set, *, phase):
        return verify_reviewed_candidate_read_set(read_set, phase=phase)

    def _build_once(self, chapter_index: int) -> TrustedChapterCandidateContext:
        root = self.story_project_root
        identity, history = _current(root, chapter=chapter_index)
        before = capture_story_project_read_set(root, chapter_index, project_identity=identity)
        validation = validate_story_project(story_project=root, chapter=chapter_index)
        if not validation.ok:
            raise ChapterCandidateContextError("; ".join(p.message for p in validation.problems if p.blocking))
        snapshot = normalize_snapshot({"chapter_index": chapter_index})
        memory = normalize_memory_context({"source": "chapter_candidate_context", "status": "ready", "items": []})
        mapped = build_story_project_runtime_context(root, chapter_index, snapshot=snapshot, memory_context=memory,
                                                     previous_chapter_fail_closed=True)
        semantic = parse_story_project_semantic_state(root, chapter_index, project_identity=identity)
        activation = evaluate_story_state_activation(identity, semantic)
        read_set = capture_story_project_read_set(root, chapter_index, project_identity=identity,
            parser_version=SEMANTIC_PARSER_VERSION, parse_status="warning" if mapped.warnings or semantic["parse_warnings"] else "ok")
        runtime = RuntimePaths.for_story_project(root)
        context = replace(mapped, chapter_resolution=validation.chapter_resolution, project_identity=identity.to_dict(),
            read_set=read_set, story_state_mode="strict", semantic_state=semantic,
            semantic_audit={**activation, "parser_version": semantic["parser_version"],
                "semantic_schema_version": semantic["schema_version"], "layout_profile_version": semantic["layout_profile_version"],
                "source_digest": semantic["source_digest"], "provenance_count": len(semantic["provenance"]),
                "blocking_conflict_count": sum(1 for p in semantic["conflicts"] if p.get("blocking")),
                "warning_count": len(semantic["parse_warnings"]), "parser_authoritative": False,
                "canonical_authoritative": False, "authority_source": "legacy_reviewed_state",
                "legacy_markdown_prompt_mode": "audit_only"},
            memory_v2={"status": "absent", "canonical_path": str(runtime.memory_dir / "v2/canonical_memory.json"),
                "event_store": str(runtime.memory_dir / "v2/events"), "revision": None, "projection_hash": None,
                "projection": None, "cache_status": RUNTIME_CACHE_POLICY_BYPASS}).to_dict()
        value = history["value"]
        context["reviewed_state"] = build_reviewed_state_context(context, authoritative_state=value["current_state"],
            source_relative_path=RELATIVE_PATH, source_sha256=history["document_sha256"],
            manual_sources_sha256=value["source_revision"]["sha256"], committed_through=value["committed_through"],
            static_references=value["static_references"],
            previous_chapter_facts=history.get("previous_chapter_facts"))
        self._validate_context_binding(context, identity=identity, chapter_index=chapter_index)
        validated_reviewed_state_context(context)
        after_identity, after_history = _current(root, chapter=chapter_index)
        verification = _verify_story_project_read_set(read_set, phase="prepare", source_identity=after_identity)
        _verify_story_project_read_set(before, phase="prepare", source_identity=after_identity)
        if (identity.to_dict() != after_identity.to_dict() or history != after_history
                or _read_set_source_binding(before) != _read_set_source_binding(read_set)):
            raise ChapterCandidateContextError("reviewed context changed while it was being read")
        digest = canonical_json_hash({"context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
            "runtime_cache_policy": RUNTIME_CACHE_POLICY_BYPASS, "story_project_context": context,
            "snapshot": snapshot, "memory_context": memory}, exclude_environment_fields=False)
        return TrustedChapterCandidateContext(chapter_index=chapter_index, story_project_context=copy.deepcopy(context),
            snapshot=snapshot, memory_context=memory, context_sha256=digest,
            authority_projection_sha256=None, source_verification=verification)
