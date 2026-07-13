from __future__ import annotations

from pathlib import Path
from typing import Any

from core.engine.persistence import PersistenceError, PersistenceTarget, reconcile_persistence


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

    def __init__(self, *, run_dir: str | Path, persistence_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.persistence_dir = Path(persistence_dir)

    def assert_ready(self, *, expected_book_id: str | None = None) -> None:
        report = reconcile_persistence(
            run_dir=self.run_dir,
            expected_book_id=expected_book_id,
            transactions_dir=self.persistence_dir,
        )
        blocking = [
            item
            for item in report.get("transactions") or []
            if isinstance(item, dict) and item.get("state") in {"commit_marked", "recovery_required"}
        ]
        if blocking:
            run_ids = ", ".join(str(item.get("run_id") or "unknown") for item in blocking)
            raise PersistenceError(f"persistence_reconciliation_required: {run_ids}")

    def anticipated(
        self,
        run_id: str,
        targets: list[PersistenceTarget],
        *,
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        journal = self.persistence_dir.resolve() / str(run_id)
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
