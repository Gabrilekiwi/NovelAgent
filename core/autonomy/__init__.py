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

__all__ = [
    "AutonomyPlanError",
    "TrustedProfiles",
    "TrustedProfilesError",
    "build_source_snapshot",
    "compile_instruction_plan",
    "validate_instruction_plan",
    "validate_source_snapshot",
]
