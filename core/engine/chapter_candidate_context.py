from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.engine.safe_paths import assert_safe_local_tree
from core.memory_v2.canonical import canonical_json_hash
from core.memory_v2.event_store import memory_projection_hash
from core.memory_v2.recovery import (
    MemoryAuthorityMismatchError,
    read_event_authority_projection,
)
from core.runtime_paths import RuntimePaths
from core.state.memory import normalize_memory_context
from core.state.snapshot import normalize_snapshot
from core.story_project.authority import AUTHORITY_MODE_EVENT
from core.story_project.identity import ProjectIdentity, load_project_identity
from core.story_project.read_set import (
    capture_story_project_read_set,
    verify_story_project_read_set,
)
from core.story_project.runtime import (
    RUNTIME_CACHE_POLICY_BYPASS,
    build_generation_story_project_context,
)
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from core.story_project.reviewed_state_context import validated_reviewed_state_context


CHAPTER_CANDIDATE_CONTEXT_VERSION = "1.0"


class ChapterCandidateContextError(ValueError):
    code = "chapter_candidate_context_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(f"{self.code}: {message}")


@dataclass(frozen=True)
class TrustedChapterCandidateContext:
    """A disk-derived, hash-bound input boundary for one chapter candidate."""

    chapter_index: int
    story_project_context: dict[str, Any]
    snapshot: dict[str, Any]
    memory_context: dict[str, Any]
    context_sha256: str
    authority_projection_sha256: str | None
    source_verification: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
            "chapter_index": self.chapter_index,
            "story_project_context": copy.deepcopy(self.story_project_context),
            "snapshot": copy.deepcopy(self.snapshot),
            "memory_context": copy.deepcopy(self.memory_context),
            "context_sha256": self.context_sha256,
            "authority_projection_sha256": self.authority_projection_sha256,
            "runtime_cache_policy": RUNTIME_CACHE_POLICY_BYPASS,
            "source_verification": copy.deepcopy(self.source_verification),
        }


class ChapterCandidateContextBuilder:
    """Build trusted Candidate inputs without adopting or repairing runtime caches."""

    def __init__(self, story_project_root: str | Path, *,
                 reviewed_state_loader: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.story_project_root = Path(story_project_root).resolve()
        if not self.story_project_root.is_dir():
            raise ChapterCandidateContextError(
                f"StoryProject root is not a directory: {self.story_project_root}"
            )
        assert_safe_local_tree(self.story_project_root)
        # Internal dependency injection for the D1 source experiment. Production
        # entry points do not provide a loader until format/upgrade gates exist.
        self.reviewed_state_loader = reviewed_state_loader

    def build(self, chapter_index: int) -> TrustedChapterCandidateContext:
        chapter_index = _validated_chapter_index(chapter_index)
        return self._build_once(chapter_index)

    def verify_binding(
        self,
        *,
        chapter_index: int,
        source: Mapping[str, Any],
        authority: Mapping[str, Any],
        phase: str,
    ) -> dict[str, Any]:
        """Verify a stored context binding without reparsing the full project."""

        chapter_index = _validated_chapter_index(chapter_index)
        read_set = source.get("story_project_read_set")
        if not isinstance(read_set, dict):
            raise ChapterCandidateContextError("stored source has no complete read set")
        root_identity = read_set.get("root_identity")
        resolved_path = (
            root_identity.get("resolved_path")
            if isinstance(root_identity, dict)
            else None
        )
        if (
            not isinstance(resolved_path, str)
            or Path(resolved_path).resolve() != self.story_project_root
            or read_set.get("chapter_index") != chapter_index
        ):
            raise ChapterCandidateContextError(
                "stored read set is bound to another project or chapter"
            )
        verification = verify_story_project_read_set(read_set, phase=phase)
        identity = self._load_persisted_identity()
        event_authority = _uses_event_authority(identity)
        identity_authority = (
            identity.authority if isinstance(identity.authority, dict) else {}
        )
        actual_authority = {
            "mode": str(identity_authority.get("mode") or "legacy_markdown_v1"),
            "authority_epoch": int(identity_authority.get("authority_epoch") or 0),
            "head_event_hash": identity_authority.get("head_event_hash"),
            "identity_revision": read_set.get("identity_revision"),
        }
        if dict(authority) != actual_authority:
            raise ChapterCandidateContextError(
                "stored authority binding differs from current ProjectIdentity",
                code="chapter_candidate_authority_changed",
            )
        if source.get("context_builder_version") != CHAPTER_CANDIDATE_CONTEXT_VERSION:
            raise ChapterCandidateContextError(
                "stored context was built by an unsupported builder version"
            )
        if source.get("runtime_cache_policy") != RUNTIME_CACHE_POLICY_BYPASS:
            raise ChapterCandidateContextError(
                "stored context is not bound to the read-only cache policy"
            )

        expected_projection_sha256 = source.get("authority_projection_sha256")
        if event_authority:
            memory_root = (
                RuntimePaths.for_story_project(self.story_project_root).memory_dir / "v2"
            )
            try:
                replayed = read_event_authority_projection(
                    memory_root,
                    expected_book_id=identity.book_id,
                    expected_authority_epoch=int(identity_authority["authority_epoch"]),
                    expected_head_event_hash=str(identity_authority["head_event_hash"]),
                )
            except MemoryAuthorityMismatchError as exc:
                raise ChapterCandidateContextError(
                    "event authority differs from its pinned ProjectIdentity",
                    code="chapter_candidate_authority_changed",
                ) from exc
            except (OSError, ValueError) as exc:
                raise ChapterCandidateContextError(
                    "event authority could not be replayed and verified",
                    code="chapter_candidate_authority_replay_failed",
                ) from exc
            actual_projection_sha256 = memory_projection_hash(replayed["projection"])
            if actual_projection_sha256 != expected_projection_sha256:
                raise ChapterCandidateContextError(
                    "event-authority projection changed after context preparation",
                    code="chapter_candidate_authority_changed",
                )
        else:
            if identity.story_state_mode != "strict":
                raise ChapterCandidateContextError(
                    "legacy StoryProject identity is no longer strict",
                    code="chapter_candidate_context_not_strict",
                )
            actual_projection_sha256 = None
            if expected_projection_sha256 is not None:
                raise ChapterCandidateContextError(
                    "legacy context contains an event projection binding"
                )
        return {
            **verification,
            "context_sha256": source.get("context_sha256"),
            "authority_projection_sha256": actual_projection_sha256,
        }

    def _build_once(self, chapter_index: int) -> TrustedChapterCandidateContext:
        identity = self._load_persisted_identity()
        event_authority = _uses_event_authority(identity)
        if not event_authority and identity.story_state_mode != "strict":
            raise ChapterCandidateContextError(
                "legacy StoryProject candidates require an activated strict identity",
                code="chapter_candidate_context_not_strict",
            )

        before_read_set = capture_story_project_read_set(
            self.story_project_root,
            chapter_index,
            project_identity=identity,
            parser_version=SEMANTIC_PARSER_VERSION,
            parse_status="ok",
        )

        # These are deterministic empty runtime inputs, not mutable runtime
        # caches. StoryProject semantics (legacy) or event replay (event_v1)
        # becomes authoritative when the context is applied by the executor.
        snapshot = normalize_snapshot({"chapter_index": chapter_index})
        memory_context = normalize_memory_context(
            {
                "source": "chapter_candidate_context",
                "status": "ready",
                "items": [],
            }
        )
        context = build_generation_story_project_context(
            story_project=self.story_project_root,
            chapter=chapter_index,
            snapshot=snapshot,
            memory_context=memory_context,
            project_identity=identity,
            runtime_cache_policy=RUNTIME_CACHE_POLICY_BYPASS,
        ).to_dict()
        self._validate_context_binding(
            context,
            identity=identity,
            chapter_index=chapter_index,
        )
        if self.reviewed_state_loader is not None:
            context["reviewed_state"] = self.reviewed_state_loader(copy.deepcopy(context))
            validated_reviewed_state_context(context)
        read_set = context["read_set"]
        verification = verify_story_project_read_set(read_set, phase="prepare")
        verify_story_project_read_set(before_read_set, phase="prepare")
        current_identity = self._load_persisted_identity()
        if (
            current_identity.to_dict() != identity.to_dict()
            or _read_set_source_binding(before_read_set)
            != _read_set_source_binding(read_set)
        ):
            raise ChapterCandidateContextError(
                "StoryProject sources or identity changed while context was being read",
                code="chapter_candidate_context_unstable",
            )

        memory_v2 = context.get("memory_v2")
        projection_sha256 = (
            memory_v2.get("projection_hash")
            if isinstance(memory_v2, dict)
            else None
        )
        if event_authority:
            projection = (
                memory_v2.get("projection")
                if isinstance(memory_v2, dict)
                else None
            )
            if not isinstance(projection, dict) or not isinstance(projection_sha256, str):
                raise ChapterCandidateContextError(
                    "event authority did not produce a replayed canonical projection",
                    code="chapter_candidate_authority_projection_missing",
                )
            if memory_v2.get("cache_status") != RUNTIME_CACHE_POLICY_BYPASS:
                raise ChapterCandidateContextError(
                    "event authority context unexpectedly adopted a runtime cache",
                    code="chapter_candidate_runtime_cache_adopted",
                )
        elif projection_sha256 is not None:
            raise ChapterCandidateContextError(
                "legacy context unexpectedly adopted a canonical-memory cache",
                code="chapter_candidate_runtime_cache_adopted",
            )

        context_sha256 = canonical_json_hash(
            {
                "context_builder_version": CHAPTER_CANDIDATE_CONTEXT_VERSION,
                "runtime_cache_policy": RUNTIME_CACHE_POLICY_BYPASS,
                "story_project_context": context,
                "snapshot": snapshot,
                "memory_context": memory_context,
            },
            exclude_environment_fields=False,
        )
        return TrustedChapterCandidateContext(
            chapter_index=chapter_index,
            story_project_context=context,
            snapshot=snapshot,
            memory_context=memory_context,
            context_sha256=context_sha256,
            authority_projection_sha256=projection_sha256,
            source_verification=verification,
        )

    def _load_persisted_identity(self) -> ProjectIdentity:
        identity = load_project_identity(self.story_project_root)
        if identity is None or identity.ephemeral:
            raise ChapterCandidateContextError(
                "chapter candidates require an existing persisted ProjectIdentity",
                code="chapter_candidate_identity_missing",
            )
        return identity

    def _validate_context_binding(
        self,
        context: dict[str, Any],
        *,
        identity: ProjectIdentity,
        chapter_index: int,
    ) -> None:
        root = context.get("story_project_root")
        if not isinstance(root, str) or Path(root).resolve() != self.story_project_root:
            raise ChapterCandidateContextError(
                "runtime context is bound to another StoryProject root"
            )
        if context.get("chapter_index") != chapter_index:
            raise ChapterCandidateContextError(
                "runtime context resolved a different chapter"
            )
        if context.get("story_state_mode") != "strict":
            raise ChapterCandidateContextError(
                "runtime context is not authoritative strict state",
                code="chapter_candidate_context_not_strict",
            )
        if context.get("project_identity") != identity.to_dict():
            raise ChapterCandidateContextError(
                "runtime context identity differs from persisted ProjectIdentity"
            )
        read_set = context.get("read_set")
        if not isinstance(read_set, dict):
            raise ChapterCandidateContextError("runtime context has no complete read set")
        if (
            read_set.get("book_id") != identity.book_id
            or read_set.get("chapter_index") != chapter_index
        ):
            raise ChapterCandidateContextError(
                "runtime context read set is bound to another book or chapter"
            )


def _validated_chapter_index(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChapterCandidateContextError("chapter_index must be a positive integer")
    return value


def _uses_event_authority(identity: ProjectIdentity) -> bool:
    authority = identity.authority
    return isinstance(authority, dict) and authority.get("mode") == AUTHORITY_MODE_EVENT


def _read_set_source_binding(read_set: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(read_set.get(field))
        for field in (
            "book_id",
            "chapter_index",
            "root_identity",
            "identity_revision",
            "parser_version",
            "entries",
            "candidate_fingerprints",
            "membership",
            "membership_fingerprint",
        )
    }


__all__ = [
    "CHAPTER_CANDIDATE_CONTEXT_VERSION",
    "ChapterCandidateContextBuilder",
    "ChapterCandidateContextError",
    "TrustedChapterCandidateContext",
]
