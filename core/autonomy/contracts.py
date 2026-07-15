"""Public names for the durable book-autonomy contracts.

The autonomy implementation predates the public names used by the reliability
plan.  This module is deliberately a compatibility layer, not a second storage
format:

* :class:`BookRunPlan` is the existing, hash-bound ``InstructionPlan`` JSON.
* :class:`ChapterOutline` is the existing, hash-bound
  ``AutonomyOutlineCheckpoint`` JSON.
* :class:`BookRunSession` is the read-only status projection rebuilt by
  :meth:`core.autonomy.session.AutonomySessionStore.status` from durable
  genesis, event, lease, arc-plan, and completion-receipt evidence.

The first two validators delegate to the original validators so legacy bytes,
schema versions, hashes, and error behavior remain authoritative.  A session
projection is never accepted as writable authority.
"""

from typing import Any, Literal, Mapping, NotRequired, Protocol, TypedDict, cast

from core.autonomy.common import (
    AutonomyContractError,
    positive_int,
    safe_id,
    sha256_digest,
    validate_mapping,
)
from core.autonomy.outline import validate_outline_checkpoint
from core.autonomy.plans import validate_instruction_plan
from core.autonomy.profiles import TrustedProfiles


class SourceSnapshot(TypedDict):
    schema_version: str
    book_id: str
    root_uuid: str
    authority_epoch: int
    authority_head_event_hash: str | None
    canonical_next_chapter: int
    source_digest: str
    captured_at: str
    snapshot_hash: str


class StoryProjectSelection(TypedDict):
    profile_id: str
    book_id: str
    root_uuid: str
    profile_hash: str


class ProviderModelSelection(TypedDict):
    profile_id: str
    provider: Literal["openai", "anthropic"]
    endpoint_type: Literal["official", "openai_compatible"]
    model: str
    max_output_tokens: int
    profile_hash: str


class FileDeliverySelection(TypedDict):
    profile_id: str
    target_kind: Literal["file"]
    root_uuid: str
    requires_run_id: bool
    requires_chapter_id: bool
    profile_hash: str


class BudgetSelection(TypedDict):
    profile_id: str
    max_chapters: int
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_wall_seconds: int
    profile_hash: str


class QualityPolicySelection(TypedDict):
    profile_id: str
    policy: Literal["minimal", "standard", "strict"]
    minimum_score: Literal[0]
    profile_hash: str


class BookRunSelections(TypedDict):
    story_project: StoryProjectSelection
    provider_model: ProviderModelSelection
    file_delivery: FileDeliverySelection
    budget: BudgetSelection
    quality_policy: QualityPolicySelection


class BookRunPlan(TypedDict):
    """Typed public view of the persisted ``InstructionPlan`` contract."""

    schema_version: Literal["1.0", "1.1"]
    plan_id: str
    plan_hash: str
    state: Literal["preview"]
    intent: Literal["generate_contiguous_canonical_chapters"]
    instruction_digest: str
    story_brief: NotRequired[str]
    profile_set_id: str
    profile_set_hash: str
    source_snapshot: SourceSnapshot
    selections: BookRunSelections
    requested_chapter_count: int
    chapter_start: int
    chapter_end: int
    created_at: str


class OutlineAuthority(TypedDict):
    epoch: int
    head_event_hash: str | None


class ChapterOutline(TypedDict):
    """Typed public view of an ``AutonomyOutlineCheckpoint`` revision.

    ``recovery_protocol`` is optional only so historical markerless v1.0
    checkpoints remain readable.  Newly stored checkpoints still require the
    current recovery protocol through ``OutlineCheckpointStore.create``.
    """

    schema_version: Literal["1.0"]
    recovery_protocol: NotRequired[Literal["commit-marker-forward-v1"]]
    checkpoint_id: str
    checkpoint_hash: str
    book_id: str
    session_id: str
    plan_id: str
    arc_plan_id: str
    chapter_index: int
    planned_target_hash: str
    source_snapshot_hash: str
    authority: OutlineAuthority
    outline_input_digest: str
    provider_profile: str
    execution_kind: Literal["model", "deterministic"]
    outline_text: str
    outline_hash: str
    canonical_relative_path: str
    canonical_before_sha256: str | None
    created_at: str


class BookRunSession(TypedDict):
    """Typed, non-authoritative projection of one durable autonomy session."""

    schema_version: Literal["1.0"]
    session_id: str
    book_id: str
    plan_id: str
    plan_hash: str
    arc_plan_id: str
    arc_plan_hash: str
    state: Literal["active", "cancelled", "abandoned", "completed"]
    lease_held: bool
    lease_hash: str | None
    event_count: int
    last_event_hash: str
    requested_chapter_count: int
    trusted_profiles_current: bool
    completed_count: int
    completed_chapters: list[int]
    canonical_next_chapter: int
    last_completion_receipt_hash: str | None
    expected_source_snapshot_hash: str
    delivery_blocked: bool
    delivery_blocked_chapters: list[int]
    finalization_required: bool
    root_remap_blocked: bool


class BookRunSessionSource(Protocol):
    """Minimum authoritative store interface used to materialize a session."""

    def status(
        self, session_id: str | None = None, *, at: str | None = None
    ) -> Mapping[str, Any]: ...


class BookRunContractError(AutonomyContractError):
    pass


def validate_book_run_plan(
    value: Any,
    *,
    trusted_profiles: TrustedProfiles | None = None,
    current_source_snapshot: Mapping[str, Any] | None = None,
) -> BookRunPlan:
    """Validate a ``BookRunPlan`` using the canonical InstructionPlan rules."""

    return cast(
        BookRunPlan,
        validate_instruction_plan(
            value,
            trusted_profiles=trusted_profiles,
            current_source_snapshot=current_source_snapshot,
        ),
    )


def validate_chapter_outline(value: Any) -> ChapterOutline:
    """Validate a ``ChapterOutline`` using the canonical checkpoint rules."""

    return cast(ChapterOutline, validate_outline_checkpoint(value))


def validate_book_run_session(value: Any) -> BookRunSession:
    """Validate the shape and cross-field invariants of a rebuilt session view.

    This validates a projection only.  Call :func:`materialize_book_run_session`
    when receipt/event/lease authority must also be established by the store.
    """

    session = validate_mapping(value, "book_run_session.schema.json", "BookRunSession")
    for field in ("session_id", "book_id", "plan_id", "arc_plan_id"):
        safe_id(field, session[field])
    for field in (
        "plan_hash",
        "arc_plan_hash",
        "last_event_hash",
        "expected_source_snapshot_hash",
    ):
        sha256_digest(field, session[field])
    sha256_digest("lease_hash", session["lease_hash"], optional=True)
    sha256_digest(
        "last_completion_receipt_hash",
        session["last_completion_receipt_hash"],
        optional=True,
    )

    event_count = positive_int("event_count", session["event_count"])
    requested = positive_int(
        "requested_chapter_count", session["requested_chapter_count"]
    )
    completed = positive_int("completed_count", session["completed_count"], minimum=0)
    canonical_next = positive_int(
        "canonical_next_chapter", session["canonical_next_chapter"]
    )
    if event_count < 1 or completed > requested:
        raise BookRunContractError(
            "book_run_session_count_invalid",
            "session event/completion counts are inconsistent with the plan",
        )

    chapters = list(session["completed_chapters"])
    first_completed = canonical_next - completed
    expected_chapters = list(range(first_completed, canonical_next))
    if first_completed < 1 or chapters != expected_chapters:
        raise BookRunContractError(
            "book_run_session_chapter_chain_invalid",
            "completed chapters must be one contiguous range ending before canonical next",
        )

    completion_hash = session["last_completion_receipt_hash"]
    if (completed == 0) != (completion_hash is None):
        raise BookRunContractError(
            "book_run_session_completion_evidence_invalid",
            "last completion receipt presence must match the rebuilt completion count",
        )

    blocked_chapters = list(session["delivery_blocked_chapters"])
    if (
        len(set(blocked_chapters)) != len(blocked_chapters)
        or blocked_chapters != sorted(blocked_chapters)
        or any(chapter not in chapters for chapter in blocked_chapters)
        or session["delivery_blocked"] != bool(blocked_chapters)
    ):
        raise BookRunContractError(
            "book_run_session_delivery_state_invalid",
            "delivery-blocked chapters must be a sorted subset of completed chapters",
        )

    expected_finalization = (
        session["state"] == "active"
        and completed == requested
        and not session["delivery_blocked"]
    )
    if session["finalization_required"] != expected_finalization:
        raise BookRunContractError(
            "book_run_session_finalization_invalid",
            "finalization flag does not match receipt-derived progress",
        )
    if session["root_remap_blocked"] != (
        session["state"] == "active" or session["lease_held"]
    ):
        raise BookRunContractError(
            "book_run_session_root_remap_invalid",
            "root remap flag does not match session/lease state",
        )
    if session["state"] == "completed" and (
        completed != requested or session["delivery_blocked"]
    ):
        raise BookRunContractError(
            "book_run_session_terminal_invalid",
            "completed sessions require every requested receipt and resolved delivery",
        )
    return cast(BookRunSession, session)


def materialize_book_run_session(
    source: BookRunSessionSource,
    session_id: str | None = None,
    *,
    at: str | None = None,
) -> BookRunSession:
    """Rebuild and validate a session through its authoritative store."""

    return validate_book_run_session(source.status(session_id, at=at))


__all__ = [
    "BookRunContractError",
    "BookRunPlan",
    "BookRunSession",
    "BookRunSessionSource",
    "ChapterOutline",
    "materialize_book_run_session",
    "validate_book_run_plan",
    "validate_book_run_session",
    "validate_chapter_outline",
]
