from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from core.engine.persistence import (
    LocalPersistenceTransaction,
    PersistenceError,
    reconcile_persistence,
)
from core.engine.persistence_v2 import PersistenceV2Transaction, reconcile_pending_persistence_v2


class PersistenceBackendError(PersistenceError):
    pass


@runtime_checkable
class PersistenceBackend(Protocol):
    """Explicit persistence backend boundary; selection never falls through."""

    backend_id: str

    def create_transaction(
        self,
        *,
        run_id: str,
        book_id: str | None = None,
        story_project_read_set: Mapping[str, Any] | None = None,
        read_set_declared_writes: Iterable[Mapping[str, Any]] = (),
        fault_injector: Callable[[str, int | None, Path | None], None] | None = None,
    ) -> Any:
        ...

    def reconcile(self, *, expected_book_id: str | None = None) -> Mapping[str, Any]:
        ...


class LegacyV1PersistenceBackend:
    backend_id = "v1"

    def __init__(
        self,
        *,
        run_dir: str | Path,
        persistence_dir: str | Path,
        allowed_roots: Iterable[str | Path],
    ) -> None:
        self.run_dir = Path(run_dir)
        self.persistence_dir = Path(persistence_dir)
        self.allowed_roots = tuple(Path(path) for path in allowed_roots)
        if not self.allowed_roots:
            raise PersistenceBackendError("v1 persistence requires at least one allowed root")

    def create_transaction(
        self,
        *,
        run_id: str,
        book_id: str | None = None,
        story_project_read_set: Mapping[str, Any] | None = None,
        read_set_declared_writes: Iterable[Mapping[str, Any]] = (),
        fault_injector: Callable[[str, int | None, Path | None], None] | None = None,
    ) -> LocalPersistenceTransaction:
        return LocalPersistenceTransaction(
            run_dir=self.run_dir,
            run_id=run_id,
            allowed_roots=self.allowed_roots,
            book_id=book_id,
            transactions_dir=self.persistence_dir,
            fault_injector=fault_injector,
            story_project_read_set=story_project_read_set,
            read_set_declared_writes=read_set_declared_writes,
        )

    def reconcile(self, *, expected_book_id: str | None = None) -> Mapping[str, Any]:
        return reconcile_persistence(
            run_dir=self.run_dir,
            expected_book_id=expected_book_id,
            transactions_dir=self.persistence_dir,
        )


class PersistenceV2Backend:
    backend_id = "v2"

    def __init__(
        self,
        *,
        transaction_root: str | Path,
        root_map: Mapping[str, str | Path],
    ) -> None:
        self.transaction_root = Path(transaction_root)
        self.root_map = dict(root_map)

    def create_transaction(
        self,
        *,
        run_id: str,
        book_id: str | None = None,
        story_project_read_set: Mapping[str, Any] | None = None,
        read_set_declared_writes: Iterable[Mapping[str, Any]] = (),
        fault_injector: Callable[[str, int | None, Path | None], None] | None = None,
    ) -> PersistenceV2Transaction:
        if not book_id:
            raise PersistenceBackendError("v2 persistence requires book_id")
        return PersistenceV2Transaction(
            transaction_root=self.transaction_root,
            run_id=run_id,
            book_id=book_id,
            root_map=self.root_map,
            fault_injector=fault_injector,
            story_project_read_set=story_project_read_set,
            read_set_declared_writes=read_set_declared_writes,
        )

    def reconcile(self, *, expected_book_id: str | None = None) -> Mapping[str, Any]:
        return reconcile_pending_persistence_v2(
            self.transaction_root,
            expected_book_id=expected_book_id,
        )


def select_persistence_backend(
    backend: str | PersistenceBackend,
    *,
    run_dir: str | Path,
    persistence_dir: str | Path,
    allowed_roots: Iterable[str | Path] = (),
    root_map: Mapping[str, str | Path] | None = None,
) -> PersistenceBackend:
    if not isinstance(backend, str):
        if not (
            isinstance(getattr(backend, "backend_id", None), str)
            and callable(getattr(backend, "create_transaction", None))
            and callable(getattr(backend, "reconcile", None))
        ):
            raise PersistenceBackendError("custom persistence backend does not satisfy PersistenceBackend")
        return backend
    if backend == "v1":
        return LegacyV1PersistenceBackend(
            run_dir=run_dir,
            persistence_dir=persistence_dir,
            allowed_roots=allowed_roots,
        )
    if backend == "v2":
        if root_map is None:
            raise PersistenceBackendError("v2 persistence requires an explicit root_map")
        return PersistenceV2Backend(transaction_root=persistence_dir, root_map=root_map)
    raise PersistenceBackendError(f"unknown persistence backend: {backend}")


LegacyPersistenceBackendAdapter = LegacyV1PersistenceBackend
PersistenceV2BackendAdapter = PersistenceV2Backend


__all__ = [
    "LegacyV1PersistenceBackend",
    "LegacyPersistenceBackendAdapter",
    "PersistenceBackend",
    "PersistenceBackendError",
    "PersistenceV2Backend",
    "PersistenceV2BackendAdapter",
    "select_persistence_backend",
]
