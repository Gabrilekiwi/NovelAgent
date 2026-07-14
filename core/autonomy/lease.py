from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    atomic_replace_json,
    canonical_hash,
    load_json_object,
    now_utc,
    parse_utc,
    positive_int,
    safe_id,
    sha256_digest,
    state_lock,
    validate_mapping,
)


class BookLeaseError(AutonomyContractError):
    pass


def build_book_lease(
    *,
    book_id: str,
    session_id: str,
    plan_id: str,
    generation: int,
    previous_lease_hash: str | None,
    acquired_at: str,
    renewed_at: str,
    expires_at: str,
    status: str = "active",
) -> dict[str, Any]:
    lease = {
        "schema_version": "1.0",
        "book_id": safe_id("book_id", book_id),
        "session_id": safe_id("session_id", session_id),
        "plan_id": safe_id("plan_id", plan_id),
        "generation": positive_int("generation", generation),
        "previous_lease_hash": sha256_digest(
            "previous_lease_hash", previous_lease_hash, optional=True
        ),
        "status": str(status),
        "acquired_at": acquired_at,
        "renewed_at": renewed_at,
        "expires_at": expires_at,
    }
    lease["lease_hash"] = canonical_hash(lease, exclude_fields=("lease_hash",))
    return validate_book_lease(lease)


def validate_book_lease(value: Any) -> dict[str, Any]:
    lease = validate_mapping(value, "book_lease.schema.json", "BookLease")
    for field in ("book_id", "session_id", "plan_id"):
        safe_id(field, lease[field])
    positive_int("generation", lease["generation"])
    sha256_digest("lease_hash", lease["lease_hash"])
    sha256_digest("previous_lease_hash", lease["previous_lease_hash"], optional=True)
    acquired = parse_utc(lease["acquired_at"])
    renewed = parse_utc(lease["renewed_at"])
    expires = parse_utc(lease["expires_at"])
    if renewed < acquired or expires < renewed:
        raise BookLeaseError(
            "book_lease_time_invalid", "lease timestamps must be monotonically ordered"
        )
    if lease["status"] == "active" and expires == renewed:
        raise BookLeaseError("book_lease_time_invalid", "active lease must have positive duration")
    expected = canonical_hash(lease, exclude_fields=("lease_hash",))
    if lease["lease_hash"] != expected:
        raise BookLeaseError("book_lease_hash_mismatch", "BookLease content was modified")
    return lease


class BookLeaseStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def acquire(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        ttl_seconds: int = 300,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._acquire_fenced(
                book_id=book_id,
                session_id=session_id,
                plan_id=plan_id,
                ttl_seconds=ttl_seconds,
                at=at,
            )

    def _acquire_fenced(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        ttl_seconds: int,
        at: str | None,
    ) -> dict[str, Any]:
        book = safe_id("book_id", book_id)
        session = safe_id("session_id", session_id)
        plan = safe_id("plan_id", plan_id)
        ttl = _ttl(ttl_seconds)
        current_path = self._current_path(book)
        timestamp = at or now_utc()
        moment = parse_utc(timestamp)
        with state_lock(self.root, current_path):
            current = self._reconcile_history_head(book)
            if current is not None and _is_active_at(current, timestamp):
                if current["session_id"] == session and current["plan_id"] == plan:
                    return current
                raise BookLeaseError(
                    "book_lease_held",
                    f"book already has active writer session {current['session_id']}",
                )
            generation = int(current["generation"]) + 1 if current else 1
            acquired = build_book_lease(
                book_id=book,
                session_id=session,
                plan_id=plan,
                generation=generation,
                previous_lease_hash=current["lease_hash"] if current else None,
                acquired_at=timestamp,
                renewed_at=timestamp,
                expires_at=(moment + timedelta(seconds=ttl)).isoformat(),
            )
            self._publish(acquired)
            return acquired

    def renew(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        expected_lease_hash: str,
        ttl_seconds: int = 300,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._renew_fenced(
                book_id=book_id,
                session_id=session_id,
                plan_id=plan_id,
                expected_lease_hash=expected_lease_hash,
                ttl_seconds=ttl_seconds,
                at=at,
            )

    def _renew_fenced(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        expected_lease_hash: str,
        ttl_seconds: int,
        at: str | None,
    ) -> dict[str, Any]:
        book = safe_id("book_id", book_id)
        current_path = self._current_path(book)
        timestamp = at or now_utc()
        moment = parse_utc(timestamp)
        ttl = _ttl(ttl_seconds)
        with state_lock(self.root, current_path):
            current = self._reconcile_history_head(book)
            if current is None:
                raise BookLeaseError("book_lease_missing", "book has no lease record")
            if current["lease_hash"] != sha256_digest(
                "expected_lease_hash", expected_lease_hash
            ):
                raise BookLeaseError("book_lease_cas_failed", "lease generation changed")
            self._assert_owner(current, session_id=session_id, plan_id=plan_id)
            if not _is_active_at(current, timestamp):
                raise BookLeaseError("book_lease_expired", "expired lease cannot be renewed")
            renewed = build_book_lease(
                book_id=book,
                session_id=current["session_id"],
                plan_id=current["plan_id"],
                generation=int(current["generation"]) + 1,
                previous_lease_hash=current["lease_hash"],
                acquired_at=current["acquired_at"],
                renewed_at=timestamp,
                expires_at=(moment + timedelta(seconds=ttl)).isoformat(),
            )
            self._publish(renewed)
            return renewed

    def release(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        expected_lease_hash: str | None = None,
        at: str | None = None,
    ) -> dict[str, Any] | None:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._release_fenced(
                book_id=book_id,
                session_id=session_id,
                plan_id=plan_id,
                expected_lease_hash=expected_lease_hash,
                at=at,
            )

    def _release_fenced(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        expected_lease_hash: str | None,
        at: str | None,
    ) -> dict[str, Any] | None:
        book = safe_id("book_id", book_id)
        current_path = self._current_path(book)
        timestamp = at or now_utc()
        with state_lock(self.root, current_path):
            current = self._reconcile_history_head(book)
            if current is None or current["status"] == "released":
                return current
            self._assert_owner(current, session_id=session_id, plan_id=plan_id)
            if expected_lease_hash is not None and current["lease_hash"] != sha256_digest(
                "expected_lease_hash", expected_lease_hash
            ):
                raise BookLeaseError("book_lease_cas_failed", "lease generation changed")
            released = build_book_lease(
                book_id=book,
                session_id=current["session_id"],
                plan_id=current["plan_id"],
                generation=int(current["generation"]) + 1,
                previous_lease_hash=current["lease_hash"],
                acquired_at=current["acquired_at"],
                renewed_at=timestamp,
                expires_at=timestamp,
                status="released",
            )
            self._publish(released)
            return released

    def assert_held(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._assert_held_remap_fenced(
                book_id=book_id,
                session_id=session_id,
                plan_id=plan_id,
                at=at,
            )

    def _assert_held_remap_fenced(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Assert while the caller already owns the runtime remap fence."""

        book = safe_id("book_id", book_id)
        with state_lock(self.root, self._current_path(book)):
            return self._assert_held_fenced(
                book_id=book,
                session_id=session_id,
                plan_id=plan_id,
                at=at,
            )

    def _assert_held_fenced(
        self,
        *,
        book_id: str,
        session_id: str,
        plan_id: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Assert a lease while the caller already owns the autonomy root lock."""

        current = self._reconcile_history_head(
            safe_id("book_id", book_id), publish=False
        )
        if current is None:
            raise BookLeaseError("book_lease_missing", "book has no lease record")
        self._assert_owner(current, session_id=session_id, plan_id=plan_id)
        if not _is_active_at(current, at or now_utc()):
            raise BookLeaseError(
                "book_lease_not_held", "outline provider call requires a live book lease"
            )
        return current

    def reconcile(self, book_id: str) -> dict[str, Any] | None:
        """Roll a unique append-only lease history head into ``current.json``.

        History is the durable side of the lease publication protocol. A
        process may stop after publishing history but before replacing the
        mutable current pointer; a unique linear descendant is therefore safe
        to roll forward. Forks and gaps are never guessed through.
        """

        with state_lock(self.root.parent / ".root-remap-fence"):
            return self._reconcile_fenced(book_id)

    def _reconcile_fenced(self, book_id: str) -> dict[str, Any] | None:
        book = safe_id("book_id", book_id)
        with state_lock(self.root, self._current_path(book)):
            return self._reconcile_history_head(book)

    def load(self, book_id: str) -> dict[str, Any]:
        book = safe_id("book_id", book_id)
        with state_lock(self.root, self._current_path(book)):
            current = self._reconcile_history_head(book)
        if current is None:
            raise BookLeaseError("book_lease_missing", "book has no lease record")
        return current

    def load_history(self, book_id: str, lease_hash: str) -> dict[str, Any]:
        """Load one fencing generation and verify its chain back to genesis."""

        book = safe_id("book_id", book_id)
        expected = sha256_digest("lease_hash", lease_hash)
        records: dict[str, dict[str, Any]] = {}
        for path in sorted(self._history_dir(book).glob("*.json")):
            record = validate_book_lease(load_json_object(path))
            if record["book_id"] != book:
                raise BookLeaseError(
                    "book_lease_history_invalid", "lease history changed book scope"
                )
            records[record["lease_hash"]] = record
        current = records.get(expected)
        if current is None:
            raise BookLeaseError(
                "book_lease_history_missing", "fencing lease generation is not durable"
            )
        generation = int(current["generation"])
        cursor = current
        while cursor["previous_lease_hash"] is not None:
            predecessor = records.get(cursor["previous_lease_hash"])
            if predecessor is None or int(predecessor["generation"]) != generation - 1:
                raise BookLeaseError(
                    "book_lease_history_invalid", "lease generation chain is broken"
                )
            if predecessor["book_id"] != book:
                raise BookLeaseError(
                    "book_lease_history_invalid", "lease history changed book scope"
                )
            cursor = predecessor
            generation -= 1
        if generation != 1:
            raise BookLeaseError(
                "book_lease_history_invalid", "lease history does not reach genesis"
            )
        return current

    def assert_descends_from(
        self, book_id: str, *, current_lease_hash: str, ancestor_lease_hash: str
    ) -> None:
        ancestor = sha256_digest("ancestor_lease_hash", ancestor_lease_hash)
        ancestor_record = self.load_history(book_id, ancestor)
        owner_scope = (
            ancestor_record["session_id"],
            ancestor_record["plan_id"],
        )
        cursor = self.load_history(book_id, current_lease_hash)
        while True:
            if (cursor["session_id"], cursor["plan_id"]) != owner_scope:
                raise BookLeaseError(
                    "book_lease_fence_owner_discontinuity",
                    "another writer generation intervened after the stage fence",
                )
            if cursor["lease_hash"] == ancestor:
                return
            previous = cursor["previous_lease_hash"]
            if previous is None:
                raise BookLeaseError(
                    "book_lease_fence_not_ancestor",
                    "stage fencing generation is not in the current lease chain",
                )
            cursor = self.load_history(book_id, previous)

    def _load_optional(self, book_id: str) -> dict[str, Any] | None:
        path = self._current_path(book_id)
        if not path.exists():
            return None
        current = validate_book_lease(load_json_object(path))
        history = self._history_dir(book_id) / _history_name(current)
        if not history.is_file() or validate_book_lease(load_json_object(history)) != current:
            raise BookLeaseError(
                "book_lease_history_missing", "current lease is not backed by append-only history"
            )
        return current

    def _reconcile_history_head(
        self, book_id: str, *, publish: bool = True
    ) -> dict[str, Any] | None:
        """Rebuild the one valid lease chain while the autonomy root lock is held."""

        history_dir = self._history_dir(book_id)
        history_paths = sorted(history_dir.glob("*.json"))
        if not history_paths:
            if self._current_path(book_id).exists():
                raise BookLeaseError(
                    "book_lease_history_missing",
                    "current lease is not backed by append-only history",
                )
            return None
        by_generation: dict[int, dict[str, Any]] = {}
        for path in history_paths:
            lease = validate_book_lease(load_json_object(path))
            if lease["book_id"] != book_id or path.name != _history_name(lease):
                raise BookLeaseError(
                    "book_lease_history_invalid",
                    "lease history path or book scope was modified",
                )
            generation = int(lease["generation"])
            existing = by_generation.get(generation)
            if existing is not None and existing["lease_hash"] != lease["lease_hash"]:
                raise BookLeaseError(
                    "book_lease_history_forked",
                    f"lease history has multiple generation {generation} records",
                )
            by_generation[generation] = lease
        maximum = max(by_generation)
        if set(by_generation) != set(range(1, maximum + 1)):
            raise BookLeaseError(
                "book_lease_history_invalid", "lease generation history has a gap"
            )
        previous: str | None = None
        for generation in range(1, maximum + 1):
            lease = by_generation[generation]
            if lease["previous_lease_hash"] != previous:
                raise BookLeaseError(
                    "book_lease_history_forked",
                    "lease generation does not descend from the preceding generation",
                )
            previous = lease["lease_hash"]
        head = by_generation[maximum]
        current_path = self._current_path(book_id)
        if current_path.exists():
            current = validate_book_lease(load_json_object(current_path))
            generation = int(current["generation"])
            durable = by_generation.get(generation)
            if durable is None or durable != current:
                raise BookLeaseError(
                    "book_lease_history_forked",
                    "current lease is not on the unique durable history chain",
                )
        if not current_path.exists() or load_json_object(current_path) != head:
            if not publish:
                raise BookLeaseError(
                    "book_lease_recovery_required",
                    "lease history is ahead of current; acquire the remap fence to recover",
                )
            atomic_replace_json(current_path, head)
        return head

    @staticmethod
    def _assert_owner(
        lease: Mapping[str, Any], *, session_id: str, plan_id: str
    ) -> None:
        if lease["session_id"] != safe_id("session_id", session_id) or lease[
            "plan_id"
        ] != safe_id("plan_id", plan_id):
            raise BookLeaseError("book_lease_owner_mismatch", "lease belongs to another session")

    def _publish(self, lease: Mapping[str, Any]) -> None:
        validated = validate_book_lease(lease)
        history = self._history_dir(validated["book_id"]) / _history_name(validated)
        atomic_append_json(history, validated)
        atomic_replace_json(self._current_path(validated["book_id"]), validated)

    def _book_directory(self, book_id: str) -> Path:
        key = canonical_hash({"book_id": safe_id("book_id", book_id)})[:16]
        return self.root / "leases" / key

    def _current_path(self, book_id: str) -> Path:
        return self._book_directory(book_id) / "current.json"

    def _history_dir(self, book_id: str) -> Path:
        return self._book_directory(book_id) / "history"


def _history_name(lease: Mapping[str, Any]) -> str:
    return f"{int(lease['generation']):06d}-{lease['lease_hash'][:20]}.json"


def _ttl(value: int) -> int:
    ttl = positive_int("ttl_seconds", value)
    if ttl > 86_400:
        raise BookLeaseError("book_lease_ttl_invalid", "lease TTL may not exceed 24 hours")
    return ttl


def _is_active_at(lease: Mapping[str, Any], timestamp: str) -> bool:
    return lease["status"] == "active" and parse_utc(lease["expires_at"]) > parse_utc(timestamp)


__all__ = [
    "BookLeaseError",
    "BookLeaseStore",
    "build_book_lease",
    "validate_book_lease",
]
