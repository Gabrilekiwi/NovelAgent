"""Shared execution gate; metadata inspection remains available independently.

D2 reserves ProjectIdentity 3.0 for reviewed legacy state. Normal readers must
continue to reject it until T3.2 installs the complete reader/writer transaction.
Do not raise CURRENT_WRITER_CONTRACT to advertise an unimplemented writer.
"""
from __future__ import annotations

from pathlib import Path

from core.story_project.authority import CURRENT_WRITER_CONTRACT, assert_authority_writer
from core.story_project.identity import ProjectIdentity, ProjectIdentityMismatchError, load_project_identity


def assert_project_execution_compatible(
    root: str | Path, *, expected_book_id: str | None = None,
) -> ProjectIdentity | None:
    """Read only, before locks/artifacts/Providers; never silently adopt a format."""
    identity = load_project_identity(root)
    if identity is None:
        # Existing onboarding paths own identity creation; this is no new permit.
        return None
    if expected_book_id is not None and identity.book_id != expected_book_id:
        raise ProjectIdentityMismatchError(
            expected_book_id=expected_book_id, actual_book_id=identity.book_id,
            source="project execution format gate")
    assert_authority_writer(
        identity, writer_mode=identity.authority["mode"],
        writer_contract=CURRENT_WRITER_CONTRACT)
    return identity
