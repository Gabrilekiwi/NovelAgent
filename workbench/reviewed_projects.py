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


class NewBookRegistry(ReviewedProjectRegistry):
    """Request-scoped creation. Existing/imported books never enter this path."""
    def __init__(self, database, *, allowed_workspaces, settings, publication):
        super().__init__(database, allowed_workspaces=allowed_workspaces)
        if not isinstance(settings, str) or len(settings) > 4000:
            raise ProjectRegistryError("开篇设定请使用不超过 4000 字的文字。")
        self.settings = settings.strip()
        self.publication = publication

    def _initialize_staged_book(self, staging, identity):
        from core.story_project.new_book import initialize_staged_book
        initialize_staged_book(staging, identity, self.settings)

    def _on_new_registration(self, connection, row):
        from pathlib import Path
        from core.engine.root_registry import RootRegistryService
        root = Path(row["root"])
        runtime = root / ".novelagent/runtime"
        with self.publication._owner(row["book_id"]).locked() as owner:
            RootRegistryService(runtime / "reviewed/p").ensure({"story_project": root, "runtime": runtime})
            owner.enroll(root=row["root"], database=self.database, project_id=row["id"], connection=connection)
