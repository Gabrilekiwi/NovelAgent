"""Internal lookup of already registered, actually upgraded reviewed books.

The normal registry and public import/create routes retain their format gate.
This class grants no publication permission or upgrade capability.
"""
from core.story_project.reviewed_identity import (
    REVIEWED_WRITER_CONTRACT, load_verified_reviewed_identity, validate_reviewed_identity,
)
from workbench.projects import ProjectRegistry, ProjectRegistryError


class ReviewedProjectRegistry(ProjectRegistry):
    def _load_registered_identity(self, root):
        identity = validate_reviewed_identity(load_verified_reviewed_identity(root)["identity"])
        if identity.authority["minimum_writer_contract"] > REVIEWED_WRITER_CONTRACT:
            raise ProjectRegistryError("reviewed identity requires a newer supported contract")
        return identity
