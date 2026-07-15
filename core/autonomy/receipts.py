from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Mapping

from core.autonomy.common import (
    AutonomyContractError,
    atomic_append_json,
    canonical_hash,
    load_json_object,
    now_utc,
    positive_int,
    safe_id,
    sha256_digest,
    state_lock,
    validate_mapping,
)
from core.autonomy.plans import (
    validate_instruction_plan,
    validate_source_snapshot,
)
from core.autonomy.lease import BookLeaseStore, validate_book_lease
from core.stage_control import (
    assert_receipt_matches_authorization,
    validate_stage_authorization,
    validate_stage_receipt,
    validate_stage_receipt_chain,
)


PublicationVerifier = Callable[[Any], Mapping[str, Any]]
DeliveryResolutionVerifier = Callable[[Mapping[str, Any]], bool]


class AutonomyReceiptError(AutonomyContractError):
    pass


class StageReceiptStore:
    """Atomic append-only storage for the shared ``StageReceipt`` contract."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.leases = BookLeaseStore(self.root)

    def append(
        self,
        receipt: Mapping[str, Any],
        *,
        authorization: Mapping[str, Any],
        expected_lease_hash: str,
        at: str | None = None,
    ) -> dict[str, Any]:
        validated = validate_stage_receipt(dict(receipt))
        authorized = validate_stage_authorization(dict(authorization))
        assert_receipt_matches_authorization(validated, authorized)
        directory = self._directory(
            validated["session_id"], int(validated["chapter_index"])
        )
        with state_lock(self.root, directory / ".append"):
            lease = self.leases._assert_held_fenced(
                book_id=validated["book_id"],
                session_id=validated["session_id"],
                plan_id=validated["plan_id"],
                at=at or now_utc(),
            )
            if lease["lease_hash"] != sha256_digest(
                "expected_lease_hash", expected_lease_hash
            ):
                raise AutonomyReceiptError(
                    "stage_receipt_lease_fence_stale", "book lease generation changed"
                )
            existing = self.load_chain(
                validated["session_id"], int(validated["chapter_index"])
            )
            if existing:
                previous_fence = self.fence_for(existing[-1])
                self.leases.assert_descends_from(
                    validated["book_id"],
                    current_lease_hash=lease["lease_hash"],
                    ancestor_lease_hash=previous_fence["lease_hash"],
                )
            for item in existing:
                if item["receipt_hash"] == validated["receipt_hash"]:
                    if item != validated:
                        raise AutonomyReceiptError(
                            "stage_receipt_replay_conflict", "receipt hash replay has different content"
                        )
                    return item
            candidate = [*existing, validated]
            validate_stage_receipt_chain(candidate)
            _validate_autonomy_stage_chain(candidate)
            sequence = len(existing) + 1
            path = directory / f"{sequence:04d}-{validated['receipt_hash'][:20]}.json"
            fence_path = directory / "fences" / f"{validated['receipt_hash'][:20]}.json"
            if fence_path.is_file():
                # A crash may leave authorization/fence evidence ahead of the
                # final receipt. Reuse the already durable timestamped fence;
                # rebuilding with ``now_utc`` would create conflicting bytes.
                fence = validate_stage_lease_fence(load_json_object(fence_path))
                expected_fence_scope = (
                    validated["receipt_hash"],
                    authorized["authorization_hash"],
                    validated["book_id"],
                    validated["session_id"],
                    validated["plan_id"],
                    validated["chapter_index"],
                    lease["lease_hash"],
                    lease["generation"],
                )
                actual_fence_scope = (
                    fence["receipt_hash"],
                    fence["authorization_hash"],
                    fence["book_id"],
                    fence["session_id"],
                    fence["plan_id"],
                    fence["chapter_index"],
                    fence["lease_hash"],
                    fence["lease_generation"],
                )
                if actual_fence_scope != expected_fence_scope:
                    raise AutonomyReceiptError(
                        "stage_receipt_fence_conflict",
                        "orphan StageReceipt fence belongs to different evidence",
                    )
            else:
                fence = build_stage_lease_fence(
                    receipt=validated,
                    authorization=authorized,
                    lease=lease,
                    fenced_at=at,
                )
            atomic_append_json(
                directory
                / "authorizations"
                / f"{authorized['authorization_hash'][:20]}.json",
                authorized,
            )
            atomic_append_json(fence_path, fence)
            atomic_append_json(path, validated)
            return validated

    def load_chain(self, session_id: str, chapter_index: int) -> list[dict[str, Any]]:
        directory = self._directory(session_id, chapter_index)
        if not directory.exists():
            return []
        receipts = [
            validate_stage_receipt(load_json_object(path))
            for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]-*.json"))
        ]
        validate_stage_receipt_chain(receipts)
        _validate_autonomy_stage_chain(receipts)
        for expected, path in enumerate(
            sorted(directory.glob("[0-9][0-9][0-9][0-9]-*.json")), start=1
        ):
            if not path.name.startswith(f"{expected:04d}-"):
                raise AutonomyReceiptError(
                    "stage_receipt_sequence_broken", "StageReceipt filenames are not contiguous"
                )
        for receipt in receipts:
            self.fence_for(receipt)
        return receipts

    def fence_for(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_stage_receipt(dict(receipt))
        directory = self._directory(
            validated["session_id"], int(validated["chapter_index"])
        )
        authorization = validate_stage_authorization(
            load_json_object(
                directory
                / "authorizations"
                / f"{validated['authorization_hash'][:20]}.json"
            )
        )
        assert_receipt_matches_authorization(validated, authorization)
        fence = validate_stage_lease_fence(
            load_json_object(
                directory / "fences" / f"{validated['receipt_hash'][:20]}.json"
            )
        )
        if (
            fence["receipt_hash"] != validated["receipt_hash"]
            or fence["authorization_hash"] != authorization["authorization_hash"]
        ):
            raise AutonomyReceiptError(
                "stage_receipt_fence_mismatch", "StageReceipt fence binding changed"
            )
        self.leases.load_history(validated["book_id"], fence["lease_hash"])
        return fence

    def authorization_for(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Load the immutable authorization bound to a stored receipt."""

        validated = validate_stage_receipt(dict(receipt))
        directory = self._directory(
            validated["session_id"], int(validated["chapter_index"])
        )
        authorization = validate_stage_authorization(
            load_json_object(
                directory
                / "authorizations"
                / f"{validated['authorization_hash'][:20]}.json"
            )
        )
        assert_receipt_matches_authorization(validated, authorization)
        return authorization

    def contains(self, receipt: Mapping[str, Any]) -> bool:
        validated = validate_stage_receipt(dict(receipt))
        return any(
            item == validated
            for item in self.load_chain(
                validated["session_id"], int(validated["chapter_index"])
            )
        )

    def _directory(self, session_id: str, chapter_index: int) -> Path:
        session_key = canonical_hash({"session_id": safe_id("session_id", session_id)})[:16]
        chapter = positive_int("chapter_index", chapter_index)
        return self.root / "stage_receipts" / session_key / f"chapter-{chapter:06d}"


def build_stage_lease_fence(
    *,
    receipt: Mapping[str, Any],
    authorization: Mapping[str, Any],
    lease: Mapping[str, Any],
    fenced_at: str | None = None,
) -> dict[str, Any]:
    validated_receipt = validate_stage_receipt(dict(receipt))
    validated_authorization = validate_stage_authorization(dict(authorization))
    validated_lease = validate_book_lease(dict(lease))
    assert_receipt_matches_authorization(validated_receipt, validated_authorization)
    scope = (
        validated_receipt["book_id"],
        validated_receipt["session_id"],
        validated_receipt["plan_id"],
    )
    if scope != (
        validated_lease["book_id"],
        validated_lease["session_id"],
        validated_lease["plan_id"],
    ):
        raise AutonomyReceiptError(
            "stage_receipt_lease_scope_mismatch", "lease belongs to another stage scope"
        )
    fence = {
        "schema_version": "1.0",
        "receipt_hash": validated_receipt["receipt_hash"],
        "authorization_hash": validated_authorization["authorization_hash"],
        "book_id": validated_receipt["book_id"],
        "session_id": validated_receipt["session_id"],
        "plan_id": validated_receipt["plan_id"],
        "chapter_index": validated_receipt["chapter_index"],
        "lease_hash": validated_lease["lease_hash"],
        "lease_generation": validated_lease["generation"],
        "fenced_at": fenced_at or now_utc(),
    }
    fence["fence_hash"] = canonical_hash(fence, exclude_fields=("fence_hash",))
    return validate_stage_lease_fence(fence)


def validate_stage_lease_fence(value: Any) -> dict[str, Any]:
    fence = validate_mapping(value, "stage_lease_fence.schema.json", "StageLeaseFence")
    for field in ("book_id", "session_id", "plan_id"):
        safe_id(field, fence[field])
    positive_int("chapter_index", fence["chapter_index"])
    positive_int("lease_generation", fence["lease_generation"])
    for field in (
        "fence_hash",
        "receipt_hash",
        "authorization_hash",
        "lease_hash",
    ):
        sha256_digest(field, fence[field])
    expected = canonical_hash(fence, exclude_fields=("fence_hash",))
    if fence["fence_hash"] != expected:
        raise AutonomyReceiptError(
            "stage_receipt_fence_hash_mismatch", "StageReceipt lease fence was modified"
        )
    return fence


def _validate_autonomy_stage_chain(receipts: list[Mapping[str, Any]]) -> None:
    if not receipts:
        return
    if receipts[0]["stage"] != "outline":
        raise AutonomyReceiptError(
            "stage_receipt_sequence_invalid", "autonomy stage chain must begin with outline"
        )
    allowed_next = {
        "outline": "scene_plan",
        "scene_plan": "draft",
        "draft": "polish",
        "polish": "validator",
        "validator": "repair",
        "repair": "validator",
    }
    for index, receipt in enumerate(receipts):
        if receipt["status"] != "succeeded" and index != len(receipts) - 1:
            raise AutonomyReceiptError(
                "stage_receipt_terminal_failure",
                "failed, uncertain, rejected, or cancelled stage terminates its chain",
            )
        if index == 0:
            continue
        previous = receipts[index - 1]
        if previous["status"] != "succeeded":
            raise AutonomyReceiptError(
                "stage_receipt_terminal_failure", "terminal stage cannot be retried in-place"
            )
        if receipt["stage"] != allowed_next[previous["stage"]]:
            raise AutonomyReceiptError(
                "stage_receipt_sequence_invalid",
                f"{previous['stage']} cannot advance directly to {receipt['stage']}",
            )


def build_chapter_completion_receipt(
    *,
    book_id: str,
    session_id: str,
    plan_id: str,
    arc_plan_id: str,
    chapter_index: int,
    planned_target_hash: str,
    chapter_body_hash: str,
    final_stage_receipt_hash: str,
    publication_receipt_hash: str,
    lease_hash: str,
    lease_generation: int,
    source_snapshot_after: Mapping[str, Any],
    previous_completion_receipt_hash: str | None,
    status: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema_version": "1.0",
        "book_id": safe_id("book_id", book_id),
        "session_id": safe_id("session_id", session_id),
        "plan_id": safe_id("plan_id", plan_id),
        "arc_plan_id": safe_id("arc_plan_id", arc_plan_id),
        "chapter_index": positive_int("chapter_index", chapter_index),
        "planned_target_hash": sha256_digest("planned_target_hash", planned_target_hash),
        "chapter_body_hash": sha256_digest("chapter_body_hash", chapter_body_hash),
        "final_stage_receipt_hash": sha256_digest(
            "final_stage_receipt_hash", final_stage_receipt_hash
        ),
        "publication_receipt_hash": sha256_digest(
            "publication_receipt_hash", publication_receipt_hash
        ),
        "lease_hash": sha256_digest("lease_hash", lease_hash),
        "lease_generation": positive_int("lease_generation", lease_generation),
        "source_snapshot_after": validate_source_snapshot(source_snapshot_after),
        "previous_completion_receipt_hash": sha256_digest(
            "previous_completion_receipt_hash",
            previous_completion_receipt_hash,
            optional=True,
        ),
        "status": str(status),
        "created_at": created_at or now_utc(),
    }
    receipt["receipt_hash"] = canonical_hash(receipt, exclude_fields=("receipt_hash",))
    return validate_chapter_completion_receipt(receipt)


def validate_chapter_completion_receipt(value: Any) -> dict[str, Any]:
    receipt = validate_mapping(
        value, "chapter_completion_receipt.schema.json", "ChapterCompletionReceipt"
    )
    for field in ("book_id", "session_id", "plan_id", "arc_plan_id"):
        safe_id(field, receipt[field])
    positive_int("chapter_index", receipt["chapter_index"])
    for field in (
        "receipt_hash",
        "planned_target_hash",
        "chapter_body_hash",
        "final_stage_receipt_hash",
        "publication_receipt_hash",
        "lease_hash",
    ):
        sha256_digest(field, receipt[field])
    positive_int("lease_generation", receipt["lease_generation"])
    sha256_digest(
        "previous_completion_receipt_hash",
        receipt["previous_completion_receipt_hash"],
        optional=True,
    )
    after = validate_source_snapshot(receipt["source_snapshot_after"])
    if after["book_id"] != receipt["book_id"] or after[
        "canonical_next_chapter"
    ] != int(receipt["chapter_index"]) + 1:
        raise AutonomyReceiptError(
            "chapter_completion_source_snapshot_invalid",
            "post-publication source snapshot must advance exactly one chapter",
        )
    expected = canonical_hash(receipt, exclude_fields=("receipt_hash",))
    if receipt["receipt_hash"] != expected:
        raise AutonomyReceiptError(
            "chapter_completion_hash_mismatch", "ChapterCompletionReceipt was modified"
        )
    return receipt


class CompletionLedger:
    def __init__(
        self,
        root: str | Path,
        *,
        instruction_plan: Mapping[str, Any],
        session_id: str,
        arc_plan_id: str,
        stage_receipts: StageReceiptStore,
        publication_verifier: PublicationVerifier | None = None,
        publication_root_map: Mapping[str, str | Path] | None = None,
        delivery_resolution_verifier: DeliveryResolutionVerifier | None = None,
    ) -> None:
        self.root = Path(root)
        self.plan = validate_instruction_plan(instruction_plan)
        self.session_id = safe_id("session_id", session_id)
        self.arc_plan_id = safe_id("arc_plan_id", arc_plan_id)
        self.stage_receipts = stage_receipts
        self.leases = stage_receipts.leases
        if publication_verifier is not None:
            self.publication_verifier = publication_verifier
        elif publication_root_map is not None:
            roots = dict(publication_root_map)
            self.publication_verifier = lambda value: _durable_publication_verifier(
                value, root_map=roots
            )
        else:
            self.publication_verifier = _unconfigured_publication_verifier
        self.delivery_resolution_verifier = delivery_resolution_verifier

    def append(
        self,
        *,
        final_stage_receipt: Mapping[str, Any],
        publication_receipt: Mapping[str, Any],
        planned_target_hash: str,
        chapter_body_hash: str,
        source_snapshot_after: Mapping[str, Any],
        status: str = "committed",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        final_stage = validate_stage_receipt(dict(final_stage_receipt))
        if final_stage["status"] != "succeeded":
            raise AutonomyReceiptError(
                "chapter_completion_stage_failed", "failed, rejected, or uncertain stages do not count"
            )
        if not self.stage_receipts.contains(final_stage):
            raise AutonomyReceiptError(
                "chapter_completion_stage_unstored", "final StageReceipt is not in the append-only chain"
            )
        stage_chain = self.stage_receipts.load_chain(
            final_stage["session_id"], int(final_stage["chapter_index"])
        )
        if not stage_chain or stage_chain[-1] != final_stage:
            raise AutonomyReceiptError(
                "chapter_completion_stage_stale",
                "only the current StageReceipt chain head can complete a chapter",
            )
        if final_stage["session_id"] != self.session_id or final_stage[
            "plan_id"
        ] != self.plan["plan_id"]:
            raise AutonomyReceiptError(
                "chapter_completion_scope_mismatch", "StageReceipt belongs to another session or plan"
            )
        publication = _verified_publication(
            publication_receipt, verifier=self.publication_verifier
        )
        if publication.get("book_id") != self.plan["source_snapshot"]["book_id"]:
            raise AutonomyReceiptError(
                "chapter_completion_publication_mismatch", "publication belongs to another book"
            )
        publication_hash = sha256_digest(
            "publication_receipt.receipt_hash", publication.get("receipt_hash")
        )
        directory = self._directory()
        with state_lock(self.root, directory / ".append"):
            # Re-read the stage head under the same root lock used by stage
            # appends; this closes the check/append race.
            stage_chain = self.stage_receipts.load_chain(
                final_stage["session_id"], int(final_stage["chapter_index"])
            )
            if not stage_chain or stage_chain[-1] != final_stage:
                raise AutonomyReceiptError(
                    "chapter_completion_stage_stale",
                    "only the current StageReceipt chain head can complete a chapter",
                )
            if final_stage["stage"] != "validator":
                raise AutonomyReceiptError(
                    "chapter_completion_stage_not_final",
                    "completion requires a successful validator stage",
                )
            if final_stage["schema_version"] == "1.0" and final_stage[
                "model_call_receipt_hash"
            ] is None:
                raise AutonomyReceiptError(
                    "chapter_completion_stage_not_final",
                    "legacy validator completion requires its original model receipt evidence",
                )
            if final_stage["schema_version"] == "1.1" and final_stage[
                "execution_kind"
            ] == "model" and not final_stage["model_call_receipt_hashes"]:
                raise AutonomyReceiptError(
                    "chapter_completion_stage_not_final",
                    "model validator completion requires durable ModelCallReceipt evidence",
                )
            if final_stage["schema_version"] == "1.1" and final_stage[
                "execution_kind"
            ] == "deterministic" and final_stage[
                "execution_evidence_hash"
            ] is None:
                raise AutonomyReceiptError(
                    "chapter_completion_stage_not_final",
                    "deterministic validator completion requires reproducible execution evidence",
                )
            stage_fence = self.stage_receipts.fence_for(final_stage)
            lease = self.leases._assert_held_fenced(
                book_id=final_stage["book_id"],
                session_id=self.session_id,
                plan_id=self.plan["plan_id"],
                at=created_at or now_utc(),
            )
            self.leases.assert_descends_from(
                final_stage["book_id"],
                current_lease_hash=lease["lease_hash"],
                ancestor_lease_hash=stage_fence["lease_hash"],
            )
            publication = _verified_publication(
                publication_receipt, verifier=self.publication_verifier
            )
            publication_hash = sha256_digest(
                "publication_receipt.receipt_hash", publication.get("receipt_hash")
            )
            chain = self.rebuild()
            chapter = int(final_stage["chapter_index"])
            expected = int(self.plan["chapter_start"]) + len(chain)
            if chapter != expected:
                if any(item["chapter_index"] == chapter for item in chain):
                    existing = next(item for item in chain if item["chapter_index"] == chapter)
                    if (
                        existing["publication_receipt_hash"] == publication_hash
                        and existing["final_stage_receipt_hash"] == final_stage["receipt_hash"]
                    ):
                        return existing
                raise AutonomyReceiptError(
                    "chapter_completion_not_canonical_next",
                    f"expected canonical next chapter {expected}, got {chapter}",
                )
            if chapter > int(self.plan["chapter_end"]):
                raise AutonomyReceiptError(
                    "chapter_completion_outside_plan", "chapter is outside the approved plan range"
                )
            after = validate_source_snapshot(source_snapshot_after)
            expected_before = (
                chain[-1]["source_snapshot_after"]
                if chain
                else self.plan["source_snapshot"]
            )
            if after["root_uuid"] != expected_before["root_uuid"]:
                raise AutonomyReceiptError(
                    "chapter_completion_source_snapshot_invalid",
                    "post-publication source snapshot changed the trusted root UUID",
                )
            if int(after["authority_epoch"]) < int(expected_before["authority_epoch"]):
                raise AutonomyReceiptError(
                    "chapter_completion_source_snapshot_invalid",
                    "post-publication authority epoch moved backwards",
                )
            final_run = publication.get("final_run")
            if isinstance(final_run, Mapping) and isinstance(final_run.get("chapter_index"), int):
                if int(final_run["chapter_index"]) != chapter:
                    raise AutonomyReceiptError(
                        "chapter_completion_publication_mismatch",
                        "publication receipt chapter does not match StageReceipt",
                    )
            receipt = build_chapter_completion_receipt(
                book_id=self.plan["source_snapshot"]["book_id"],
                session_id=self.session_id,
                plan_id=self.plan["plan_id"],
                arc_plan_id=self.arc_plan_id,
                chapter_index=chapter,
                planned_target_hash=planned_target_hash,
                chapter_body_hash=chapter_body_hash,
                final_stage_receipt_hash=final_stage["receipt_hash"],
                publication_receipt_hash=publication_hash,
                lease_hash=lease["lease_hash"],
                lease_generation=lease["generation"],
                source_snapshot_after=source_snapshot_after,
                previous_completion_receipt_hash=(
                    chain[-1]["receipt_hash"] if chain else None
                ),
                status=status,
                created_at=created_at,
            )
            atomic_append_json(
                directory / "publications" / f"{publication_hash[:20]}.json", publication
            )
            sequence = len(chain) + 1
            atomic_append_json(
                directory
                / "receipts"
                / f"{sequence:06d}-{chapter:06d}-{receipt['receipt_hash'][:20]}.json",
                receipt,
            )
            return receipt

    def rebuild(self) -> list[dict[str, Any]]:
        directory = self._directory()
        receipt_paths = sorted(
            (directory / "receipts").glob("[0-9][0-9][0-9][0-9][0-9][0-9]-*.json")
        )
        chain: list[dict[str, Any]] = []
        previous: str | None = None
        expected_chapter = int(self.plan["chapter_start"])
        expected_source = self.plan["source_snapshot"]
        for sequence, path in enumerate(receipt_paths, start=1):
            if not path.name.startswith(f"{sequence:06d}-{expected_chapter:06d}-"):
                raise AutonomyReceiptError(
                    "chapter_completion_sequence_broken", "completion receipt sequence skipped"
                )
            receipt = validate_chapter_completion_receipt(load_json_object(path))
            expected_scope = (
                self.plan["source_snapshot"]["book_id"],
                self.session_id,
                self.plan["plan_id"],
                self.arc_plan_id,
            )
            actual_scope = (
                receipt["book_id"],
                receipt["session_id"],
                receipt["plan_id"],
                receipt["arc_plan_id"],
            )
            if actual_scope != expected_scope:
                raise AutonomyReceiptError(
                    "chapter_completion_scope_mismatch", "completion receipt chain changed scope"
                )
            if receipt["chapter_index"] != expected_chapter:
                raise AutonomyReceiptError(
                    "chapter_completion_not_contiguous", "completion chain skipped a chapter"
                )
            if receipt["previous_completion_receipt_hash"] != previous:
                raise AutonomyReceiptError(
                    "chapter_completion_chain_broken", "completion receipt predecessor changed"
                )
            after = receipt["source_snapshot_after"]
            if after["root_uuid"] != expected_source["root_uuid"] or int(
                after["authority_epoch"]
            ) < int(expected_source["authority_epoch"]):
                raise AutonomyReceiptError(
                    "chapter_completion_source_snapshot_invalid",
                    "completion source snapshot chain changed root or regressed authority",
                )
            publication_path = (
                directory
                / "publications"
                / f"{receipt['publication_receipt_hash'][:20]}.json"
            )
            publication = _verified_publication(
                load_json_object(publication_path), verifier=self.publication_verifier
            )
            if publication.get("receipt_hash") != receipt["publication_receipt_hash"]:
                raise AutonomyReceiptError(
                    "chapter_completion_publication_mismatch", "publication receipt hash changed"
                )
            lease = self.leases.load_history(receipt["book_id"], receipt["lease_hash"])
            if int(lease["generation"]) != int(receipt["lease_generation"]):
                raise AutonomyReceiptError(
                    "chapter_completion_lease_fence_invalid",
                    "completion receipt lease generation changed",
                )
            chain.append(receipt)
            previous = receipt["receipt_hash"]
            expected_source = after
            expected_chapter += 1
        return chain

    def load_publication(self, publication_receipt_hash: str) -> dict[str, Any]:
        """Load and durably verify a publication bound into this ledger."""

        digest = sha256_digest(
            "publication_receipt_hash", publication_receipt_hash
        )
        publication = _verified_publication(
            load_json_object(
                self._directory()
                / "publications"
                / f"{digest[:20]}.json"
            ),
            verifier=self.publication_verifier,
        )
        if publication.get("receipt_hash") != digest:
            raise AutonomyReceiptError(
                "chapter_completion_publication_mismatch",
                "publication receipt hash changed",
            )
        return dict(publication)

    def summary(self) -> dict[str, Any]:
        chain = self.rebuild()
        next_chapter = int(self.plan["chapter_start"]) + len(chain)
        blocked_chapters = []
        for item in chain:
            if item["status"] != "local_committed_delivery_blocked":
                continue
            resolved = False
            if self.delivery_resolution_verifier is not None:
                try:
                    resolved = bool(self.delivery_resolution_verifier(copy.deepcopy(item)))
                except Exception as exc:
                    raise AutonomyReceiptError(
                        "chapter_completion_delivery_resolution_invalid",
                        f"delivery resolution evidence could not be verified: {type(exc).__name__}: {exc}",
                    ) from exc
            if not resolved:
                blocked_chapters.append(item["chapter_index"])
        return {
            "completed_count": len(chain),
            "completed_chapters": [item["chapter_index"] for item in chain],
            "canonical_next_chapter": next_chapter,
            "last_completion_receipt_hash": chain[-1]["receipt_hash"] if chain else None,
            "expected_source_snapshot_hash": self.expected_source_snapshot()["snapshot_hash"],
            "delivery_blocked": bool(blocked_chapters),
            "delivery_blocked_chapters": blocked_chapters,
        }

    def expected_source_snapshot(self) -> dict[str, Any]:
        chain = self.rebuild()
        if chain:
            return copy.deepcopy(chain[-1]["source_snapshot_after"])
        return copy.deepcopy(self.plan["source_snapshot"])

    def _directory(self) -> Path:
        key = canonical_hash(
            {"session_id": self.session_id, "plan_hash": self.plan["plan_hash"]}
        )[:16]
        return self.root / "completion_ledgers" / key


def _verified_publication(
    value: Mapping[str, Any], *, verifier: PublicationVerifier
) -> dict[str, Any]:
    try:
        verified = verifier(copy.deepcopy(dict(value)))
    except Exception as exc:
        raise AutonomyReceiptError(
            "chapter_completion_publication_invalid",
            f"PublicationReceipt verification failed: {type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(verified, Mapping):
        raise AutonomyReceiptError(
            "chapter_completion_publication_invalid", "publication verifier returned no receipt"
        )
    return copy.deepcopy(dict(verified))


def _durable_publication_verifier(
    value: Any, *, root_map: Mapping[str, str | Path]
) -> Mapping[str, Any]:
    from core.engine.persistence_v2 import (
        validate_publication_receipt,
        verify_publication_receipt,
    )

    verification = verify_publication_receipt(value, root_map=root_map)
    if not verification.get("valid") or not verification.get("committed"):
        raise AutonomyReceiptError(
            "chapter_completion_publication_not_committed",
            "PublicationReceipt does not resolve to a durable commit marker",
        )
    return validate_publication_receipt(value)


def _unconfigured_publication_verifier(value: Any) -> Mapping[str, Any]:
    del value
    raise AutonomyReceiptError(
        "chapter_completion_publication_roots_required",
        "durable PublicationReceipt verification requires trusted root bindings",
    )


__all__ = [
    "AutonomyReceiptError",
    "CompletionLedger",
    "StageReceiptStore",
    "build_chapter_completion_receipt",
    "validate_chapter_completion_receipt",
]
