from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.engine.persistence import PersistenceError, PersistenceTarget
from core.engine.persistence_backends import PersistenceBackend, select_persistence_backend


PERSISTENCE_PUBLIC_FIELDS = frozenset(
    {
        "run_id",
        "state",
        "committed",
        "partial",
        "journal_path",
        "commit_marker",
        "targets",
        "errors",
        "candidate_result_path",
        "publication",
    }
)


class PersistenceCoordinator:
    """Owns persistence readiness checks and public transaction projections."""

    def __init__(
        self,
        *,
        run_dir: str | Path,
        persistence_dir: str | Path,
        backend: str | PersistenceBackend = "v1",
        allowed_roots: Iterable[str | Path] = (),
        root_map: Mapping[str, str | Path] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.persistence_dir = Path(persistence_dir)
        roots = tuple(allowed_roots) or (self.run_dir, self.persistence_dir)
        self.backend = select_persistence_backend(
            backend,
            run_dir=self.run_dir,
            persistence_dir=self.persistence_dir,
            allowed_roots=roots,
            root_map=root_map,
        )

    def assert_ready(self, *, expected_book_id: str | None = None) -> None:
        # Deliberately no fallback: an exception from the selected backend is
        # part of its recovery contract and must remain visible.
        report = self.backend.reconcile(expected_book_id=expected_book_id)
        blocking = [
            item
            for item in report.get("transactions") or []
            if isinstance(item, dict) and item.get("state") in {"commit_marked", "recovery_required"}
        ]
        if blocking:
            run_ids = ", ".join(str(item.get("run_id") or "unknown") for item in blocking)
            raise PersistenceError(f"persistence_reconciliation_required: {run_ids}")

    @property
    def backend_id(self) -> str:
        return self.backend.backend_id

    def create_transaction(
        self,
        *,
        run_id: str,
        book_id: str | None = None,
        story_project_read_set: Mapping[str, Any] | None = None,
        read_set_declared_writes: Iterable[Mapping[str, Any]] = (),
        fault_injector: Callable[[str, int | None, Path | None], None] | None = None,
    ) -> Any:
        return self.backend.create_transaction(
            run_id=run_id,
            book_id=book_id,
            story_project_read_set=story_project_read_set,
            read_set_declared_writes=read_set_declared_writes,
            fault_injector=fault_injector,
        )

    def anticipated(
        self,
        run_id: str,
        targets: list[PersistenceTarget],
        *,
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        journal_root = self.persistence_dir.resolve()
        journal = (
            journal_root / "journals" / str(run_id)
            if self.backend_id == "v2"
            else journal_root / str(run_id)
        )
        return {
            "run_id": str(run_id),
            "state": "completed",
            "committed": True,
            "partial": False,
            "journal_path": str(journal),
            "commit_marker": str(journal / "commit.marker"),
            "targets": [
                {
                    "kind": target.kind,
                    "path": str(Path(target.path).resolve()),
                    "status": "verified",
                    "metadata": dict(target.metadata),
                }
                for target in targets
            ],
            "errors": [],
            "candidate_result_path": str(journal / "candidate_result.json"),
            "publication": publication,
        }

    @staticmethod
    def attach(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        public = {key: value for key, value in payload.items() if key in PERSISTENCE_PUBLIC_FIELDS}
        result["persistence"] = public
        result["run"]["persistence"] = public
        return public


__all__ = ["PERSISTENCE_PUBLIC_FIELDS", "PersistenceCoordinator"]
