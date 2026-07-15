"""Durable, local-only autonomy planning primitives.

The package deliberately stops at authorization and durable orchestration.
It never calls a model provider or an external delivery adapter itself.
"""

from core.autonomy.plans import (
    AutonomyPlanError,
    build_source_snapshot,
    compile_instruction_plan,
    validate_instruction_plan,
    validate_source_snapshot,
)
from core.autonomy.profiles import TrustedProfiles, TrustedProfilesError
from core.autonomy.contracts import (
    BookRunContractError,
    BookRunPlan,
    BookRunSession,
    BookRunSessionSource,
    ChapterOutline,
    materialize_book_run_session,
    validate_book_run_plan,
    validate_book_run_session,
    validate_chapter_outline,
)

__all__ = [
    "AutonomyPlanError",
    "BookRunContractError",
    "BookRunPlan",
    "BookRunSession",
    "BookRunSessionSource",
    "ChapterOutline",
    "TrustedProfiles",
    "TrustedProfilesError",
    "build_source_snapshot",
    "compile_instruction_plan",
    "materialize_book_run_session",
    "validate_book_run_plan",
    "validate_book_run_session",
    "validate_chapter_outline",
    "validate_instruction_plan",
    "validate_source_snapshot",
]
