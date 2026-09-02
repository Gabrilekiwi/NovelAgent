from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from api.contracts import CHAPTER_CONTRACT, ModelOutputError, validate_language_output
from core.engine.locked_chapter_state import (
    LOCKED_CHAPTER_RESOLUTION_VERSION,
    active_locked_chapter_checkpoint,
    discarded_run_ids,
    resolved_execution_ids,
    validate_locked_chapter_resolution,
    verify_locked_chapter_scene_sources,
)
from core.engine.persistence import atomic_create_json, persistence_run_lock
from core.engine.run_record import validate_run_result
from core.engine.scene_source_provenance import (
    SceneSourceProvenanceError,
    normalize_scene_response,
    verified_scene_source_provenance,
    verified_scene_source_response,
    verify_scene_source_provenance,
)
from core.memory_v2.canonical import canonical_json_hash
from core.model_calls import (
    ModelCallStore,
    model_response_artifact_hash,
    parse_scene_generation_call_id,
)
from core.quality.final_artifact_integrity import FinalArtifactIntegrityGate
from core.schema import validate_schema
from core.state.snapshot import load_snapshot
from core.story_project.coverage import build_blueprint_plan
from core.story_project.paths import scan_prose_chapters


class LockedChapterRecoveryError(RuntimeError):
    """The locked chapter cannot be classified safely without user intervention."""


def recover_locked_chapter(
    *,
    story_project_root: str | Path,
    run_dir: str | Path,
    snapshot_path: str | Path,
    expected_book_id: str,
    language: str | None = None,
    manual_draft_path: str | Path | None = None,
    force_reset: bool = False,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Classify one locked chapter and write an append-only recovery checkpoint.

    This operation never calls a model and never changes the chapter snapshot or
    formal prose.  It only marks unresolved executions as handled and preserves
    hash-verified output that the next normal run may reuse.
    """

    story_root = Path(story_project_root).resolve(strict=True)
    runtime_root = Path(run_dir).resolve()
    snapshot_target = Path(snapshot_path).resolve(strict=True)
    now = clock or (lambda: datetime.now(timezone.utc))
    make_id = id_factory or (lambda: uuid.uuid4().hex[:12])
    if force_reset and manual_draft_path is not None:
        raise LockedChapterRecoveryError(
            "forced reset cannot be combined with a manual recovery draft"
        )

    with persistence_run_lock(
        runtime_root,
        state_paths=(snapshot_target,),
        require_existing_root=True,
    ):
        snapshot = load_snapshot(snapshot_target)
        chapter_index = int(snapshot.get("chapter_index") or 1)
        committed_paths = scan_prose_chapters(story_root).get(chapter_index, ())
        if committed_paths:
            raise LockedChapterRecoveryError(
                f"chapter {chapter_index} already has formal prose; locked-chapter recovery refuses to modify it"
            )

        unresolved = _unresolved_executions(runtime_root)
        runs = _load_runs(runtime_root, expected_book_id=expected_book_id)
        active_checkpoint = active_locked_chapter_checkpoint(
            runtime_root,
            chapter_index=chapter_index,
            expected_book_id=expected_book_id,
            language=language,
        )
        if unresolved:
            owners = _bind_unresolved_to_runs(
                unresolved,
                runs,
                chapter_index=chapter_index,
                expected_book_id=expected_book_id,
            )
            source = max(owners, key=lambda item: _run_sort_key(item["payload"], item["path"]))
            source_payload = source["payload"]
            complete_draft = _usable_complete_draft(
                source_payload,
                language=language,
                run_dir=runtime_root,
            )
        elif (
            (force_reset or manual_draft_path is not None)
            and active_checkpoint is not None
        ):
            source = next(
                (
                    item
                    for item in runs
                    if str(item["payload"]["run"].get("id"))
                    == str(active_checkpoint["source_run_id"])
                ),
                None,
            )
            if source is None:
                raise LockedChapterRecoveryError(
                    "the active recovery checkpoint source run is unavailable; "
                    "refusing an unbound reset or manual draft"
                )
            source_payload = source["payload"]
            complete_draft = None
        else:
            terminal_source = _newer_recoverable_terminal_run(
                runs,
                run_dir=runtime_root,
                chapter_index=chapter_index,
                active_checkpoint=active_checkpoint,
                language=language,
            )
            if terminal_source is None:
                return _already_recovered_result(chapter_index, active_checkpoint)
            source = terminal_source
            source_payload = source["payload"]
            complete_draft = source["complete_draft"]

        source_run = source_payload["run"]
        expected_scene_count = _expected_scene_count(source_run)
        execution_dir = _source_execution_dir(runtime_root, source)
        recovered_scenes = [
            dict(scene)
            for scene in source.get("recovered_scenes", [])
            if isinstance(scene, dict)
        ]
        if not recovered_scenes and not force_reset:
            recovered_scenes = _recover_persisted_scene_sequence(
                source_payload,
                run_dir=runtime_root,
                expected_scene_count=expected_scene_count,
                language=language,
            )
        accepted_final_draft = _complete_draft_matches_accepted_final_artifact(
            source_payload,
            complete_draft,
            run_dir=runtime_root,
        )
        if (
            not recovered_scenes
            and not force_reset
            and not accepted_final_draft
            and execution_dir is not None
            and _structured_scene_prefix_is_safe_fallback(source_payload)
        ):
            # Receipt-backed structured Scene responses retain the exact prose,
            # events and state deltas needed to rebuild the chapter pipeline.
            # Prefer that auditable prefix over an unaccepted polish/repair
            # transform, whose output may be the candidate that caused the run
            # to stop (for example, a non-converging repair).
            recovered_scenes = _recover_scene_prefix(
                execution_dir,
                active_checkpoint=active_checkpoint,
                expected_scene_count=expected_scene_count,
                language=language,
                source_run_id=str(source_run["id"]),
                expected_chapter_index=chapter_index,
                expected_book_id=expected_book_id,
            )
        if recovered_scenes:
            complete_draft = None
        elif not force_reset:
            # The RunRecord's final artifact is newer than any individual
            # transform response.  In particular, scene repair responses can
            # be structured patches whose application and final normalization
            # happen after the provider call.  Never replace a hash-gated final
            # candidate with an older polish response merely because that raw
            # response happens to look like complete prose.
            if not accepted_final_draft:
                durable_transform = _latest_usable_complete_transform(
                    execution_dir,
                    language=language,
                    excluded_stages=(
                        {"scene_repair"}
                        if _repair_did_not_converge(source_payload)
                        else set()
                    ),
                )
                if durable_transform is not None:
                    complete_draft = durable_transform
            complete_draft = _inherit_active_draft_provenance(
                complete_draft,
                active_checkpoint,
            )
        manual_draft = None
        if manual_draft_path is not None:
            manual_draft = _load_manual_recovery_draft(
                manual_draft_path,
                story_root=story_root,
                source_payload=source_payload,
                language=language,
            )
            complete_draft = manual_draft
            recovered_scenes = []

        if (
            not force_reset
            and active_checkpoint is not None
            and int(active_checkpoint["expected_scene_count"]) != expected_scene_count
        ):
            raise LockedChapterRecoveryError(
                "the existing recovery checkpoint no longer matches the chapter outline; reset it before reusing output"
            )

        if force_reset:
            complete_draft = None
            recovered_scenes = []
            action = "reset"
            reason = "operator_requested_reset"
        elif complete_draft is not None:
            recovered_scenes = []
            action = "repair_draft"
            if manual_draft is not None:
                reason = "manual_repaired_draft_provided"
            elif complete_draft.get("source_stage") == "claude_polish":
                reason = "durable_polished_draft_available"
            elif complete_draft.get("source_stage") == "scene_repair":
                reason = "durable_repaired_draft_available"
            else:
                reason = (
                    "complete_draft_available"
                    if unresolved
                    else "complete_failed_draft_available"
                )
        else:
            if not recovered_scenes and execution_dir is not None:
                recovered_scenes = _recover_scene_prefix(
                    execution_dir,
                    active_checkpoint=active_checkpoint,
                    expected_scene_count=expected_scene_count,
                    language=language,
                    source_run_id=str(source_run["id"]),
                    expected_chapter_index=chapter_index,
                    expected_book_id=expected_book_id,
                )
            if recovered_scenes:
                action = "resume_scenes"
                reason = (
                    "complete_scene_sequence_requires_revalidation"
                    if len(recovered_scenes) >= expected_scene_count
                    else "contiguous_scene_prefix_available"
                )
            else:
                action = "reset"
                reason = "no_trustworthy_content"

        all_uncommitted_current_runs = [
            str(item["payload"]["run"]["id"])
            for item in runs
            if item["payload"]["run"].get("committed") is not True
            and int(item["payload"]["run"].get("chapter_index") or 0) == chapter_index
        ]
        created_at = now().astimezone(timezone.utc)
        marker_id = f"resolution_{created_at.strftime('%Y%m%dT%H%M%S%fZ')}_{make_id()}"
        marker = {
            "schema_version": LOCKED_CHAPTER_RESOLUTION_VERSION,
            "id": marker_id,
            "created_at": created_at.isoformat(),
            "book_id": expected_book_id,
            "chapter_index": chapter_index,
            "action": action,
            "source_run_id": str(source_run["id"]),
            "resolved_execution_ids": sorted(item["execution_id"] for item in unresolved),
            "resolved_attempt_ids": sorted(
                call["attempt_id"] for item in unresolved for call in item["calls"]
            ),
            "discarded_run_ids": sorted(set(all_uncommitted_current_runs)) if action == "reset" else [],
            "expected_scene_count": expected_scene_count,
            "complete_draft": complete_draft,
            "scenes": recovered_scenes,
            "reason": reason,
        }
        marker["resolution_hash"] = canonical_json_hash(
            marker,
            exclude_fields=("resolution_hash",),
            exclude_environment_fields=False,
        )
        marker = validate_locked_chapter_resolution(marker)
        verify_locked_chapter_scene_sources(
            runtime_root,
            marker,
            language=language,
        )
        marker_path = runtime_root / "locked_chapter_resolutions" / f"{marker_id}.json"
        atomic_create_json(marker_path, marker)
        return _public_result(marker, marker_path)


def _load_manual_recovery_draft(
    path: str | Path,
    *,
    story_root: Path,
    source_payload: dict[str, Any],
    language: str | None,
) -> dict[str, Any]:
    candidate = Path(path).resolve(strict=True)
    if not candidate.is_file():
        raise LockedChapterRecoveryError("manual recovery draft must be a file")
    try:
        candidate.relative_to(story_root)
    except ValueError as exc:
        raise LockedChapterRecoveryError(
            "manual recovery draft must stay inside the StoryProject root"
        ) from exc
    try:
        raw = candidate.read_text(encoding="utf-8")
        text = validate_language_output(raw, CHAPTER_CONTRACT, language=language)
    except (OSError, UnicodeError, ModelOutputError) as exc:
        raise LockedChapterRecoveryError(
            f"manual recovery draft is not valid chapter prose: {exc}"
        ) from exc
    if len(text.strip()) < 500:
        raise LockedChapterRecoveryError(
            "manual recovery draft is too short to be treated as a complete chapter"
        )
    integrity = FinalArtifactIntegrityGate().evaluate(
        artifact_text=text,
        stage="manual_recovery",
    )
    if not integrity["accepted"]:
        codes = ", ".join(
            str(item["code"])
            for item in integrity["findings"]
            if item["blocking"]
        )
        raise LockedChapterRecoveryError(
            "manual recovery draft failed final artifact integrity: "
            f"{codes or 'unknown'}"
        )
    validation = source_payload.get("validation")
    problems = validation.get("problems") if isinstance(validation, dict) else []
    problem_codes = [
        str(item.get("code") or "")
        for item in problems or []
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    ]
    return {
        "text": text,
        "sha256": _content_sha256(text),
        "problem_codes": list(dict.fromkeys(problem_codes)),
        # Preserve repaired-draft workflow semantics: skip generation/polish and
        # run a focused validation of the previously reported problem codes.
        "source_stage": "scene_repair",
    }


def _unresolved_executions(run_dir: Path) -> list[dict[str, Any]]:
    handled = resolved_execution_ids(run_dir)
    executions_root = run_dir / "executions"
    unresolved: list[dict[str, Any]] = []
    if not executions_root.is_dir():
        return unresolved
    for execution_dir in sorted(executions_root.glob("execution_*")):
        execution_id = execution_dir.name
        if execution_id in handled:
            continue
        model_root = execution_dir / "model_calls"
        if not model_root.is_dir():
            continue
        calls = ModelCallStore(model_root).list_uncertain_calls()
        if calls:
            unresolved.append(
                {
                    "execution_id": execution_id,
                    "execution_dir": execution_dir,
                    "calls": calls,
                }
            )
    return unresolved


def _load_runs(run_dir: Path, *, expected_book_id: str) -> list[dict[str, Any]]:
    ignored = discarded_run_ids(run_dir)
    results: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("chapter_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            validate_run_result(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        run = payload["run"]
        if str(run.get("id") or "") in ignored:
            continue
        book_id = _run_book_id(run)
        if book_id is not None and book_id != expected_book_id:
            continue
        results.append({"path": path, "payload": payload})
    return results


def _newer_recoverable_terminal_run(
    runs: list[dict[str, Any]],
    *,
    run_dir: Path,
    chapter_index: int,
    active_checkpoint: dict[str, Any] | None,
    language: str | None,
) -> dict[str, Any] | None:
    checkpoint_created_at = _parse_timestamp(
        active_checkpoint.get("created_at") if isinstance(active_checkpoint, dict) else None
    )
    checkpoint_source_run_id = (
        str(active_checkpoint.get("source_run_id") or "")
        if isinstance(active_checkpoint, dict)
        else ""
    )
    candidates = sorted(
        (
            item
            for item in runs
            if item["payload"]["run"].get("committed") is not True
            and item["payload"]["run"].get("status") in {"failed", "rejected"}
            and int(item["payload"]["run"].get("chapter_index") or 0) == chapter_index
        ),
        key=lambda item: _run_sort_key(item["payload"], item["path"]),
        reverse=True,
    )
    for item in candidates:
        run = item["payload"]["run"]
        if str(run.get("id") or "") == checkpoint_source_run_id:
            preferred = _usable_complete_draft(
                item["payload"],
                language=language,
                run_dir=run_dir,
            )
            active_draft = (
                active_checkpoint.get("complete_draft")
                if isinstance(active_checkpoint, dict)
                else None
            )
            upgradeable_reason = str(
                active_checkpoint.get("reason")
                if isinstance(active_checkpoint, dict)
                else ""
            )
            if (
                not _complete_draft_matches_accepted_final_artifact(
                    item["payload"],
                    preferred,
                    run_dir=run_dir,
                )
                or not isinstance(active_draft, dict)
                or active_draft.get("sha256") == preferred.get("sha256")
                or upgradeable_reason
                not in {
                    "complete_draft_available",
                    "complete_failed_draft_available",
                    "durable_polished_draft_available",
                }
            ):
                continue
            return {
                **item,
                "complete_draft": preferred,
                "recovered_scenes": [],
            }
        run_finished_at = _parse_timestamp(run.get("finished_at") or run.get("started_at"))
        if (
            checkpoint_created_at is not None
            and run_finished_at is not None
            and run_finished_at <= checkpoint_created_at
        ):
            continue
        recovered_scenes = _recover_persisted_scene_sequence(
            item["payload"],
            run_dir=run_dir,
            expected_scene_count=_expected_scene_count(run),
            language=language,
        )
        if recovered_scenes:
            return {
                **item,
                "complete_draft": None,
                "recovered_scenes": recovered_scenes,
            }
        lineage_source = _recover_linked_checkpoint_scene_source(
            runs,
            terminal=item,
            run_dir=run_dir,
            chapter_index=chapter_index,
            active_checkpoint=active_checkpoint,
            language=language,
        )
        if lineage_source is not None:
            return lineage_source
        complete_draft = _usable_complete_draft(
            item["payload"],
            language=language,
            run_dir=run_dir,
        )
        if complete_draft is not None:
            return {**item, "complete_draft": complete_draft}
        execution_dir = _source_execution_dir(run_dir, item)
        if execution_dir is None:
            continue
        recovered_scenes = _recover_scene_prefix(
            execution_dir,
            active_checkpoint=active_checkpoint,
            expected_scene_count=_expected_scene_count(run),
            language=language,
            source_run_id=str(run["id"]),
            expected_chapter_index=int(run.get("chapter_index") or 0),
            expected_book_id=_run_book_id(run),
        )
        if recovered_scenes:
            return {
                **item,
                "complete_draft": None,
                "execution_dir": execution_dir,
                "recovered_scenes": recovered_scenes,
            }
    return None


def _recover_linked_checkpoint_scene_source(
    runs: list[dict[str, Any]],
    *,
    terminal: dict[str, Any],
    run_dir: Path,
    chapter_index: int,
    active_checkpoint: dict[str, Any] | None,
    language: str | None,
) -> dict[str, Any] | None:
    """Recover Scene evidence lost by a legacy complete-draft checkpoint.

    Older recovery code could downgrade a hash-verified merged chapter to a
    ``repair_draft`` when validation stopped before the final artifact gate.
    A later rejected run explicitly links back to that source through its
    recovery context.  Follow only that exact, hash-bound lineage and recover
    the source Scenes so the normal pipeline can revalidate and realign them.
    """

    if (
        not isinstance(active_checkpoint, dict)
        or active_checkpoint.get("action") != "repair_draft"
    ):
        return None
    checkpoint_draft = active_checkpoint.get("complete_draft")
    if (
        not isinstance(checkpoint_draft, dict)
        or checkpoint_draft.get("source_stage") != "chapter"
    ):
        return None
    checkpoint_text = checkpoint_draft.get("text")
    checkpoint_sha256 = checkpoint_draft.get("sha256")
    if (
        not isinstance(checkpoint_text, str)
        or not isinstance(checkpoint_sha256, str)
        or _content_sha256(checkpoint_text) != checkpoint_sha256
    ):
        return None

    terminal_run = terminal.get("payload", {}).get("run")
    recovery = (
        terminal_run.get("recovery_context")
        if isinstance(terminal_run, dict)
        else None
    )
    terminal_chapter = (
        terminal_run.get("chapter") if isinstance(terminal_run, dict) else None
    )
    if isinstance(terminal_chapter, dict) and "pipeline" in terminal_chapter:
        return None
    source_run_id = str(active_checkpoint.get("source_run_id") or "")
    if (
        not isinstance(recovery, dict)
        or recovery.get("available") is not True
        or str(recovery.get("source_run_id") or "") != source_run_id
        or int(recovery.get("source_chapter_index") or 0) != chapter_index
        or recovery.get("source_committed") is not False
    ):
        return None

    source = next(
        (
            item
            for item in runs
            if str(item["payload"]["run"].get("id") or "") == source_run_id
        ),
        None,
    )
    if source is None:
        return None
    source_run = source["payload"]["run"]
    source_chapter = source["payload"].get("chapter")
    source_status = str(source_run.get("status") or "")
    source_book_id = _run_book_id(source_run)
    terminal_book_id = _run_book_id(terminal_run)
    checkpoint_book_id = str(active_checkpoint.get("book_id") or "")
    if (
        source_run.get("committed") is True
        or source_status not in {"failed", "rejected"}
        or str(recovery.get("source_status") or "") != source_status
        or int(source_run.get("chapter_index") or 0) != chapter_index
        or source_chapter != checkpoint_text
        or source_book_id != checkpoint_book_id
        or terminal_book_id != checkpoint_book_id
    ):
        return None
    source_finished_at = _parse_timestamp(
        source_run.get("finished_at") or source_run.get("started_at")
    )
    checkpoint_created_at = _parse_timestamp(active_checkpoint.get("created_at"))
    terminal_finished_at = _parse_timestamp(
        terminal_run.get("finished_at") or terminal_run.get("started_at")
    )
    if (
        source_finished_at is None
        or checkpoint_created_at is None
        or terminal_finished_at is None
        or source_finished_at > checkpoint_created_at
        or checkpoint_created_at >= terminal_finished_at
    ):
        return None
    try:
        expected_scene_count = _expected_scene_count(source_run)
        terminal_scene_count = _expected_scene_count(terminal_run)
    except LockedChapterRecoveryError:
        return None
    if (
        int(active_checkpoint.get("expected_scene_count") or 0) != expected_scene_count
        or terminal_scene_count != expected_scene_count
    ):
        return None
    recovered_scenes = _recover_persisted_scene_sequence(
        source["payload"],
        run_dir=run_dir,
        expected_scene_count=expected_scene_count,
        language=language,
    )
    if not recovered_scenes:
        return None
    return {
        **source,
        "complete_draft": None,
        "recovered_scenes": recovered_scenes,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bind_unresolved_to_runs(
    unresolved: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    chapter_index: int,
    expected_book_id: str,
) -> list[dict[str, Any]]:
    by_execution: dict[str, dict[str, Any]] = {}
    for item in runs:
        run = item["payload"]["run"]
        evidence = run.get("execution_evidence")
        execution_id = evidence.get("execution_id") if isinstance(evidence, dict) else None
        if isinstance(execution_id, str):
            by_execution[execution_id] = item

    owners: list[dict[str, Any]] = []
    for execution in unresolved:
        owner = by_execution.get(execution["execution_id"])
        if owner is None:
            raise LockedChapterRecoveryError(
                "an unresolved model execution has no durable chapter run; automatic recovery stopped safely"
            )
        run = owner["payload"]["run"]
        if run.get("committed") is True or int(run.get("chapter_index") or 0) != chapter_index:
            raise LockedChapterRecoveryError(
                "the lock does not belong exclusively to the current uncommitted chapter"
            )
        book_id = _run_book_id(run)
        if book_id is not None and book_id != expected_book_id:
            raise LockedChapterRecoveryError("the locked execution belongs to a different book")
        owners.append({**owner, **execution})
    return owners


def _usable_complete_draft(
    payload: dict[str, Any],
    *,
    language: str | None,
    run_dir: Path,
) -> dict[str, Any] | None:
    value = payload.get("chapter")
    if not isinstance(value, str) or len(value.strip()) < 500:
        return None
    try:
        text = validate_language_output(value, CHAPTER_CONTRACT, language=language)
    except ModelOutputError:
        return None
    validation = payload.get("validation")
    run = payload.get("run")
    run_validation = run.get("validation") if isinstance(run, dict) else None
    problem_codes: list[str] = []
    for codes_value in (
        validation.get("problem_codes") if isinstance(validation, dict) else None,
        run_validation.get("problem_codes") if isinstance(run_validation, dict) else None,
    ):
        if isinstance(codes_value, list):
            problem_codes.extend(
                str(code) for code in codes_value if str(code).strip()
            )
    if isinstance(validation, dict):
        problem_codes.extend(
            str(item.get("code") or "")
            for item in validation.get("problems") or []
            if isinstance(item, dict) and str(item.get("code") or "").strip()
        )
    draft = {
        "text": text,
        "sha256": _content_sha256(text),
        "problem_codes": list(dict.fromkeys(problem_codes)),
        "source_stage": "chapter",
    }
    # The final gate hashes the exact persisted artifact bytes while the
    # chapter contract may normalize one terminal newline before reuse.  Bind
    # trust to the raw RunRecord value, then store the normalized text/hash in
    # the checkpoint.
    final_stage = _accepted_final_artifact_source_stage(
        payload,
        _content_sha256(value),
        normalized_sha256=draft["sha256"],
        run_dir=run_dir,
    )
    if final_stage is not None:
        draft["source_stage"] = final_stage
    return draft


def _complete_draft_matches_accepted_final_artifact(
    payload: dict[str, Any],
    complete_draft: dict[str, Any] | None,
    *,
    run_dir: Path,
) -> bool:
    if not isinstance(complete_draft, dict):
        return False
    sha256 = complete_draft.get("sha256")
    if not isinstance(sha256, str):
        return False
    raw = payload.get("chapter")
    if not isinstance(raw, str):
        return False
    try:
        normalized = validate_language_output(raw, CHAPTER_CONTRACT)
    except ModelOutputError:
        return False
    if normalized != complete_draft.get("text") or _content_sha256(normalized) != sha256:
        return False
    return (
        _accepted_final_artifact_source_stage(
            payload,
            _content_sha256(raw),
            normalized_sha256=sha256,
            run_dir=run_dir,
        )
        is not None
    )


def verified_final_recovery_draft(
    *,
    run_dir: str | Path,
    run_id: str,
    language: str | None = None,
) -> dict[str, Any] | None:
    """Return a strict, normalized final candidate for recovery coordination."""

    runtime_root = Path(run_dir).resolve(strict=True)
    candidate_id = str(run_id or "").strip()
    if not candidate_id or Path(candidate_id).name != candidate_id:
        return None
    path = runtime_root / f"{candidate_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        validate_run_result(payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    run = payload.get("run")
    if not isinstance(run, dict) or str(run.get("id") or "") != candidate_id:
        return None
    draft = _usable_complete_draft(
        payload,
        language=language,
        run_dir=runtime_root,
    )
    if not _complete_draft_matches_accepted_final_artifact(
        payload,
        draft,
        run_dir=runtime_root,
    ):
        return None
    return json.loads(json.dumps(draft, ensure_ascii=False))


def _accepted_final_artifact_source_stage(
    payload: dict[str, Any],
    chapter_sha256: str,
    *,
    normalized_sha256: str,
    run_dir: Path,
) -> str | None:
    """Return the latest completed prose stage for a hash-gated final draft.

    The returned value controls which transforms a recovery run may skip.  A
    final artifact produced after repair must resume at validation, not regress
    to a prior raw polish response and not pay for another polish call.
    """

    raw_chapter = payload.get("chapter")
    run = payload.get("run")
    run_chapter = run.get("chapter") if isinstance(run, dict) else None
    pipeline = (
        run_chapter.get("pipeline")
        if isinstance(run_chapter, dict)
        else None
    )
    if (
        not isinstance(raw_chapter, str)
        or not isinstance(run, dict)
        or run.get("status") not in {"failed", "rejected"}
        or run.get("accepted") is not False
        or run.get("committed") is not False
        or not isinstance(run_chapter, dict)
        or not isinstance(pipeline, dict)
        or _content_sha256(raw_chapter) != chapter_sha256
        or int(run_chapter.get("chars") or -1) != len(raw_chapter)
        or not _verified_rejected_chapter_artifact(
            payload,
            run_dir=run_dir,
        )
        or not _source_execution_is_settled(payload, run_dir=run_dir)
    ):
        return None

    integrity = pipeline.get("integrity")
    if not isinstance(integrity, dict):
        return None

    final_candidates = (
        run_chapter.get("final_artifact"),
        pipeline.get("final_artifact"),
        integrity.get("final_gate"),
    )
    if any(
        not _accepted_integrity_candidate(
            candidate,
            chapter_sha256=chapter_sha256,
            required_hash_field="artifact_sha256",
        )
        or candidate.get("stage") != "final_gate"
        or int(candidate.get("artifact_chars") or -1) != len(raw_chapter)
        or int(candidate.get("artifact_bytes") or -1)
        != len(raw_chapter.encode("utf-8"))
        for candidate in final_candidates
        if isinstance(candidate, dict)
    ) or not all(isinstance(candidate, dict) for candidate in final_candidates):
        return None

    records = run_chapter.get("integrity_records")
    pipeline_records = pipeline.get("integrity_records")
    audit = run_chapter.get("integrity_audit")
    if (
        not isinstance(records, list)
        or not records
        or records != pipeline_records
        or not _integrity_audit_matches_records(
            audit,
            records,
            chapter_sha256=chapter_sha256,
        )
        or not any(
            isinstance(item, dict)
            and item.get("stage") == "final_gate"
            and item.get("accepted") is True
            and item.get("output_sha256") == chapter_sha256
            for item in records
        )
    ):
        return None

    if normalized_sha256 != chapter_sha256 and not any(
        isinstance(item, dict)
        and item.get("stage") == "writeback_canonicalization"
        and item.get("accepted") is True
        and item.get("input_sha256") == normalized_sha256
        and item.get("output_sha256") == chapter_sha256
        for item in records
    ):
        return None

    if _verified_final_scene_evidence(
        payload,
        run_dir=run_dir,
        language=None,
    ) is None:
        return None

    for record_stage, checkpoint_stage, integrity_stage in (
        ("repair", "scene_repair", "repair"),
        ("polish", "claude_polish", "polish"),
        ("merge", "chapter", "merge"),
    ):
        matching_record = any(
            isinstance(item, dict)
            and item.get("stage") == record_stage
            and item.get("accepted") is True
            and item.get("output_sha256") == normalized_sha256
            for item in records
        )
        report = integrity.get(integrity_stage)
        if matching_record and _accepted_integrity_candidate(
            report,
            chapter_sha256=normalized_sha256,
            required_hash_field="artifact_sha256",
        ):
            return checkpoint_stage
    if normalized_sha256 == chapter_sha256:
        return "chapter"
    return None


def _integrity_audit_matches_records(
    audit: Any,
    records: list[Any],
    *,
    chapter_sha256: str,
) -> bool:
    if (
        not isinstance(audit, dict)
        or audit.get("final_hash_matches_gate") is not True
        or audit.get("final_chapter_sha256") != chapter_sha256
        or audit.get("final_gate_sha256") != chapter_sha256
        or int(audit.get("record_count") or -1) != len(records)
    ):
        return False
    stage_hashes = audit.get("stage_hashes")
    if not isinstance(stage_hashes, list) or len(stage_hashes) != len(records):
        return False
    for record, summary in zip(records, stage_hashes):
        if not isinstance(record, dict) or not isinstance(summary, dict):
            return False
        if (
            summary.get("stage") != record.get("stage")
            or summary.get("input_sha256") != record.get("input_sha256")
            or summary.get("output_sha256") != record.get("output_sha256")
            or bool(summary.get("accepted")) != bool(record.get("accepted"))
            or summary.get("record_sha256")
            != canonical_json_hash(record, exclude_environment_fields=False)
        ):
            return False
    return True


def _verified_rejected_chapter_artifact(
    payload: dict[str, Any],
    *,
    run_dir: Path,
) -> bool:
    raw_chapter = payload.get("chapter")
    run = payload.get("run")
    run_chapter = run.get("chapter") if isinstance(run, dict) else None
    metadata = run_chapter.get("artifact") if isinstance(run_chapter, dict) else None
    if (
        not isinstance(raw_chapter, str)
        or not isinstance(run, dict)
        or not isinstance(metadata, dict)
        or metadata.get("format") != "markdown"
        or int(metadata.get("chars") or -1) != len(raw_chapter)
    ):
        return False
    raw_path = metadata.get("path")
    expected_sha256 = metadata.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(expected_sha256, str):
        return False
    try:
        runtime_root = run_dir.resolve(strict=True).parent
        path = Path(raw_path).resolve(strict=True)
        path.relative_to(runtime_root)
        if path.parent != (runtime_root / "chapters").resolve(strict=True):
            return False
        raw_bytes = path.read_bytes()
        artifact_text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return False
    expected_text = (
        f"# Chapter {run['chapter_index']}\n\n"
        f"- Run: `{run['id']}`\n"
        f"- Status: `{run['status']}`\n"
        f"- Committed: `{run['committed']}`\n"
        f"- Repair Attempts: `{run.get('repair_attempts', 0)}`\n\n"
        "---\n\n"
        f"{raw_chapter.strip()}\n"
    )
    return (
        hashlib.sha256(raw_bytes).hexdigest() == expected_sha256
        and artifact_text == expected_text
    )


def _source_execution_is_settled(
    payload: dict[str, Any],
    *,
    run_dir: Path,
) -> bool:
    execution_dir = _source_execution_dir(run_dir, {"payload": payload})
    if execution_dir is None:
        return False
    store = ModelCallStore(execution_dir / "model_calls")
    if not store.intents_dir.is_dir():
        return False
    intent_paths = sorted(store.intents_dir.glob("*.json"))
    if not intent_paths:
        return False
    try:
        for path in intent_paths:
            intent = store.load_intent(path.stem)
            if not store.has_receipt(str(intent["attempt_id"])):
                return False
            store.load_receipt(str(intent["attempt_id"]))
    except (OSError, ValueError, KeyError):
        return False
    return True


def _verified_final_scene_evidence(
    payload: dict[str, Any],
    *,
    run_dir: Path,
    language: str | None,
) -> dict[str, Any] | None:
    """Prove final Scene text and authority data back to settled responses.

    The final Scene artifacts contain text after polish, repair and canonical
    writeback.  Provider responses contain the original merge text plus the
    events and state deltas.  Recovery may combine them only when the original
    responses reconstruct the accepted merge hash and the integrity/evidence
    chain connects that merge to the exact final chapter.
    """

    raw_chapter = payload.get("chapter")
    run = payload.get("run")
    run_chapter = run.get("chapter") if isinstance(run, dict) else None
    pipeline = (
        run_chapter.get("pipeline")
        if isinstance(run_chapter, dict)
        else None
    )
    artifacts = pipeline.get("artifacts") if isinstance(pipeline, dict) else None
    if (
        not isinstance(raw_chapter, str)
        or not isinstance(run, dict)
        or not isinstance(run_chapter, dict)
        or not isinstance(pipeline, dict)
        or not isinstance(artifacts, dict)
    ):
        return None
    run_id = str(run.get("id") or "")
    try:
        chapter_index = int(run.get("chapter_index") or 0)
    except (TypeError, ValueError):
        return None
    if not run_id or chapter_index <= 0:
        return None

    plan = _load_verified_named_json_artifact(
        artifacts.get("plan"),
        run_dir=run_dir,
        expected_name=f"chapter_plan_{chapter_index:04d}_{run_id}.json",
    )
    continuity = _load_verified_named_json_artifact(
        artifacts.get("scene_continuity"),
        run_dir=run_dir,
        expected_name=f"scene_continuity_{chapter_index:04d}_{run_id}.json",
    )
    scene_artifacts = artifacts.get("scene_drafts")
    plan_scenes = plan.get("scenes") if isinstance(plan, dict) else None
    continuity_scenes = (
        continuity.get("scenes") if isinstance(continuity, dict) else None
    )
    spans = pipeline.get("scene_spans")
    sources = pipeline.get("scene_sources")
    if not all(
        isinstance(value, list)
        for value in (
            plan_scenes,
            continuity_scenes,
            spans,
            sources,
            scene_artifacts,
        )
    ):
        return None
    expected_count = _expected_scene_count(run)
    if expected_count <= 0 or any(
        len(value) != expected_count
        for value in (
            plan_scenes,
            continuity_scenes,
            spans,
            sources,
            scene_artifacts,
        )
    ):
        return None

    by_plan = _indexed_scene_mapping(plan_scenes)
    by_continuity = _indexed_scene_mapping(continuity_scenes)
    by_span = _indexed_scene_mapping(spans)
    by_source = _indexed_scene_mapping(sources, field="scene_index")
    expected_indexes = set(range(1, expected_count + 1))
    if any(
        set(mapping) != expected_indexes
        for mapping in (by_plan, by_continuity, by_span, by_source)
    ):
        return None
    expected_scopes = [
        {
            "index": scene.get("index"),
            "required_event_ids": scene.get("required_event_ids") or [],
            "forbidden_event_ids": scene.get("forbidden_event_ids") or [],
            "planned_events": scene.get("planned_events") or [],
        }
        for scene in plan_scenes
        if isinstance(scene, dict)
    ]
    if continuity.get("planned_scene_scopes") != expected_scopes:
        return None

    final_scenes: list[dict[str, Any]] = []
    source_texts: list[str] = []
    previous_end = 0
    for index in range(1, expected_count + 1):
        span = by_span[index]
        try:
            start = int(span["start_char"])
            end = int(span["end_char"])
            chars = int(span["chars"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            start != previous_end
            or end <= start
            or end - start != chars
            or end > len(raw_chapter)
            or (index == expected_count and end != len(raw_chapter))
        ):
            return None
        text = raw_chapter[start:end]
        previous_end = end
        if index < expected_count:
            if raw_chapter[previous_end:previous_end + 2] != "\n\n":
                return None
            previous_end += 2

        source = by_source[index]
        final_sha256 = _content_sha256(text)
        if source.get("text_sha256") != final_sha256:
            return None
        planned = by_plan[index]
        artifact_metadata = scene_artifacts[index - 1]
        expected_name = f"scene_{chapter_index:04d}_{index:02d}_{run_id}.md"
        artifact_path = _verified_named_pipeline_artifact_path(
            artifact_metadata,
            run_dir=run_dir,
            expected_name=expected_name,
            expected_format="markdown",
        )
        if artifact_path is None:
            return None
        try:
            artifact_text = artifact_path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        expected_artifact = (
            f"# Scene {index}\n\n"
            f"- Run: `{run_id}`\n"
            f"- Chapter: `{chapter_index}`\n"
            f"- Goal: {planned.get('goal')}\n"
            f"- Merged Span: `{start}-{end}`\n\n"
            "---\n\n"
            f"{text.strip()}\n"
        )
        if artifact_text != expected_artifact:
            return None

        source_provenance = _verified_scene_source_from_run(
            run_dir,
            run,
            source,
        )
        if source_provenance is None:
            return None
        source_response = _verified_persisted_scene_response(
            run_dir,
            source_provenance,
            language=language,
        )
        evidence = by_continuity[index]
        raw_deltas = evidence.get("deltas")
        if (
            not isinstance(source_response, dict)
            or not isinstance(source_response.get("text"), str)
            or not isinstance(raw_deltas, dict)
            or source_response.get("events") != evidence.get("events")
            or source_response.get("deltas") != raw_deltas
            or source_response.get("continuity_note")
            != evidence.get("continuity_note")
        ):
            return None
        source_texts.append(str(source_response["text"]))

        draft: dict[str, Any] = {
            "index": index,
            "goal": str(planned.get("goal") or f"Scene {index}"),
            "text": text,
            "source_call_id": source_provenance["call_id"],
            "source_attempt_id": source_provenance["attempt_id"],
            "source_provenance": source_provenance,
            "events": [
                dict(item)
                for item in evidence.get("events") or []
                if isinstance(item, dict)
            ],
            "deltas": {
                key: [
                    dict(item)
                    for item in raw_deltas.get(key) or []
                    if isinstance(item, dict)
                ]
                for key in (
                    "characters",
                    "relationships",
                    "rosters",
                    "locations",
                    "inventory",
                    "counters",
                )
            },
            "continuity_note": str(evidence.get("continuity_note") or ""),
        }
        for field in (
            "scene_state_before",
            "scene_state_after",
            "boundary_validation",
            "evidence_alignment",
        ):
            if isinstance(evidence.get(field), dict):
                draft[field] = json.loads(
                    json.dumps(evidence[field], ensure_ascii=False)
                )
        evidence_revision = evidence.get("evidence_revision")
        if (
            isinstance(evidence_revision, int)
            and not isinstance(evidence_revision, bool)
            and evidence_revision > 0
        ):
            draft["evidence_revision"] = evidence_revision
        required_beat_indexes = planned.get("required_beat_indexes")
        if isinstance(required_beat_indexes, list):
            draft["covered_beat_indexes"] = [
                int(value)
                for value in required_beat_indexes
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            ]
        final_scenes.append(draft)

    if "\n\n".join(item["text"] for item in final_scenes) != raw_chapter:
        return None
    source_merge = "\n\n".join(source_texts)
    if not _verified_final_transform_chain(
        run_chapter,
        pipeline,
        source_merge_sha256=_content_sha256(source_merge),
        final_sha256=_content_sha256(raw_chapter),
    ):
        return None
    if continuity.get("evidence_history") != pipeline.get("scene_evidence_history"):
        return None
    return {
        "plan": plan,
        "continuity": continuity,
        "scene_drafts": final_scenes,
        "scene_spans": [dict(by_span[index]) for index in range(1, expected_count + 1)],
        "source_merge_sha256": _content_sha256(source_merge),
    }


def _verified_final_transform_chain(
    run_chapter: dict[str, Any],
    pipeline: dict[str, Any],
    *,
    source_merge_sha256: str,
    final_sha256: str,
) -> bool:
    records = run_chapter.get("integrity_records")
    pipeline_records = pipeline.get("integrity_records")
    audit = run_chapter.get("integrity_audit")
    integrity = pipeline.get("integrity")
    if (
        not isinstance(records, list)
        or len(records) < 2
        or records != pipeline_records
        or not isinstance(integrity, dict)
        or not _integrity_audit_matches_records(
            audit,
            records,
            chapter_sha256=final_sha256,
        )
    ):
        return False
    first = records[0]
    last = records[-1]
    if (
        not isinstance(first, dict)
        or first.get("stage") != "merge"
        or first.get("accepted") is not True
        or first.get("input_sha256") is not None
        or first.get("output_sha256") != source_merge_sha256
        or first.get("artifact_sha256") != source_merge_sha256
        or not isinstance(last, dict)
        or last.get("stage") != "final_gate"
        or last.get("accepted") is not True
        or last.get("input_sha256") is not None
        or last.get("output_sha256") != final_sha256
        or last.get("artifact_sha256") != final_sha256
    ):
        return False
    merge_report = integrity.get("merge")
    final_report = integrity.get("final_gate")
    if not _accepted_integrity_candidate(
        merge_report,
        chapter_sha256=source_merge_sha256,
        required_hash_field="artifact_sha256",
    ) or not _accepted_integrity_candidate(
        final_report,
        chapter_sha256=final_sha256,
        required_hash_field="artifact_sha256",
    ):
        return False

    allowed_order = {
        "polish": 1,
        "repair": 2,
        "writeback_canonicalization": 3,
    }
    transforms = records[1:-1]
    previous_sha256 = source_merge_sha256
    previous_order = 0
    for record in transforms:
        if not isinstance(record, dict):
            return False
        stage = str(record.get("stage") or "")
        order = allowed_order.get(stage)
        output_sha256 = record.get("output_sha256")
        if (
            order is None
            or order <= previous_order
            or record.get("accepted") is not True
            or record.get("input_sha256") != previous_sha256
            or not isinstance(output_sha256, str)
            or len(output_sha256) != 64
            or record.get("artifact_sha256") != output_sha256
            or not _accepted_integrity_candidate(
                integrity.get(stage),
                chapter_sha256=output_sha256,
                required_hash_field="artifact_sha256",
            )
        ):
            return False
        previous_order = order
        previous_sha256 = output_sha256
    if previous_sha256 != final_sha256:
        return False

    history = pipeline.get("scene_evidence_history")
    if not isinstance(history, list) or len(history) != len(transforms):
        return False
    for evidence, record in zip(history, transforms):
        if not isinstance(evidence, dict) or not isinstance(record, dict):
            return False
        patch_sha256 = evidence.get("patch_sha256")
        modified = evidence.get("modified_scene_indexes")
        try:
            operation_count = int(evidence.get("operation_count"))
        except (TypeError, ValueError):
            return False
        if (
            evidence.get("stage") != record.get("stage")
            or evidence.get("before_sha256") != record.get("input_sha256")
            or evidence.get("after_sha256") != record.get("output_sha256")
            or not isinstance(patch_sha256, str)
            or len(patch_sha256) != 64
            or operation_count < 0
            or not isinstance(modified, list)
        ):
            return False
    return True


def _scene_plan_matches_current_blueprint(
    payload: dict[str, Any],
    *,
    plan: dict[str, Any],
    chapter_blueprint: dict[str, Any],
) -> bool:
    if not isinstance(chapter_blueprint, dict):
        return False
    try:
        expected_plan = build_blueprint_plan(chapter_blueprint)
        expected_scenes = expected_plan.get("scenes") or []
        actual_scenes = plan.get("scenes") or []
        projected_plan = {
            "goal": plan.get("goal"),
            "scenes": [
                {
                    key: actual.get(key)
                    for key in expected
                }
                for actual, expected in zip(actual_scenes, expected_scenes)
                if isinstance(actual, dict) and isinstance(expected, dict)
            ],
        }
        plan_hash = canonical_json_hash(
            projected_plan,
            exclude_environment_fields=False,
        )
        expected_hash = canonical_json_hash(
            expected_plan,
            exclude_environment_fields=False,
        )
    except (TypeError, ValueError):
        return False
    run = payload.get("run")
    input_pack = run.get("input_pack") if isinstance(run, dict) else None
    metadata = input_pack.get("metadata") if isinstance(input_pack, dict) else None
    story = metadata.get("story_project") if isinstance(metadata, dict) else None
    if not isinstance(story, dict):
        return False
    recorded_hash = story.get("scene_plan_hash")
    return (
        len(actual_scenes) == len(expected_scenes)
        and projected_plan == expected_plan
        and plan_hash == expected_hash
        and recorded_hash == expected_hash
        and int(story.get("planned_scene_count") or 0)
        == len(expected_plan.get("scenes") or [])
    )


def _verified_named_pipeline_artifact_path(
    metadata: Any,
    *,
    run_dir: Path,
    expected_name: str,
    expected_format: str,
) -> Path | None:
    if not isinstance(metadata, dict) or metadata.get("format") != expected_format:
        return None
    path = _verified_run_artifact_path(metadata, run_dir=run_dir)
    if path is None:
        return None
    try:
        expected_parent = (run_dir / "chapter_pipeline").resolve(strict=True)
    except OSError:
        return None
    if path.parent != expected_parent or path.name != expected_name:
        return None
    return path


def _load_verified_named_json_artifact(
    metadata: Any,
    *,
    run_dir: Path,
    expected_name: str,
) -> dict[str, Any] | None:
    path = _verified_named_pipeline_artifact_path(
        metadata,
        run_dir=run_dir,
        expected_name=expected_name,
        expected_format="json",
    )
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_verified_locked_chapter_pipeline(
    *,
    run_dir: str | Path,
    checkpoint: dict[str, Any],
    chapter_blueprint: dict[str, Any],
    language: str | None = None,
) -> dict[str, Any]:
    """Rehydrate final Scene evidence for a hash-bound complete draft.

    Complete-draft recovery must not discard the Scene events and deltas that
    authorize StoryProject writeback.  This loader reconstructs the final
    Scene texts from the accepted final chapter and the persisted final spans,
    while taking structured evidence only from hash-verified runtime artifacts.
    It never calls a provider and never writes to the runtime.
    """

    runtime_root = Path(run_dir).resolve(strict=True)
    source_run_id = str(checkpoint.get("source_run_id") or "")
    complete_draft = checkpoint.get("complete_draft")
    if not source_run_id or not isinstance(complete_draft, dict):
        raise LockedChapterRecoveryError(
            "complete-draft recovery has no source run or draft evidence"
        )
    source_path = runtime_root / f"{source_run_id}.json"
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        validate_run_result(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise LockedChapterRecoveryError(
            "complete-draft source RunRecord is unavailable or invalid"
        ) from exc
    run = payload["run"]
    if (
        str(run.get("id") or "") != source_run_id
        or int(run.get("chapter_index") or 0)
        != int(checkpoint.get("chapter_index") or 0)
        or (_run_book_id(run) or str(checkpoint.get("book_id") or ""))
        != str(checkpoint.get("book_id") or "")
    ):
        raise LockedChapterRecoveryError(
            "complete-draft source RunRecord does not match its checkpoint"
        )

    source_draft = _usable_complete_draft(
        payload,
        language=language,
        run_dir=runtime_root,
    )
    if not _complete_draft_matches_accepted_final_artifact(
        payload,
        source_draft,
        run_dir=runtime_root,
    ):
        raise LockedChapterRecoveryError(
            "complete-draft source has no verified accepted final artifact"
        )
    if (
        checkpoint.get("reason") != "manual_repaired_draft_provided"
        and (
            not isinstance(source_draft, dict)
            or complete_draft.get("text") != source_draft.get("text")
            or complete_draft.get("sha256") != source_draft.get("sha256")
        )
    ):
        raise LockedChapterRecoveryError(
            "automatic complete-draft checkpoint was superseded by a newer verified final candidate"
        )
    checkpoint_text = complete_draft.get("text")
    checkpoint_sha256 = complete_draft.get("sha256")
    if (
        not isinstance(checkpoint_text, str)
        or _content_sha256(checkpoint_text) != checkpoint_sha256
    ):
        raise LockedChapterRecoveryError("complete-draft checkpoint hash mismatch")
    try:
        validate_language_output(
            checkpoint_text,
            CHAPTER_CONTRACT,
            language=language,
        )
    except ModelOutputError as exc:
        raise LockedChapterRecoveryError(
            "complete-draft checkpoint is not valid chapter prose"
        ) from exc

    verified = _verified_final_scene_evidence(
        payload,
        run_dir=runtime_root,
        language=language,
    )
    if not isinstance(verified, dict):
        raise LockedChapterRecoveryError(
            "complete-draft source Scene evidence does not match its provider receipts and final artifact"
        )
    plan = verified["plan"]
    if not _scene_plan_matches_current_blueprint(
        payload,
        plan=plan,
        chapter_blueprint=chapter_blueprint,
    ):
        raise LockedChapterRecoveryError(
            "complete-draft Scene plan no longer matches the approved chapter blueprint"
        )
    raw_chapter = payload["chapter"]
    run_chapter = run["chapter"]
    summary = run_chapter["pipeline"]
    scene_drafts = verified["scene_drafts"]

    pipeline: dict[str, Any] = {
        "chapter_index": int(run["chapter_index"]),
        "plan": plan,
        "scene_drafts": scene_drafts,
        "merged_chapter": raw_chapter,
        "scene_spans": [dict(item) for item in verified["scene_spans"]],
        "stages": [dict(item) for item in summary.get("stages") or []],
    }
    for field in (
        "story_project",
        "chapter_blueprint",
        "blueprint_coverage",
        "context_budget",
        "authoritative_state_validation",
        "integrity",
        "integrity_records",
        "scene_evidence_history",
        "scene_boundary_validations",
        "scene_state_final",
        "artifacts",
    ):
        if field in summary:
            pipeline[field] = json.loads(
                json.dumps(summary[field], ensure_ascii=False)
            )
    try:
        validate_schema(pipeline, "chapter_pipeline.schema.json")
    except ValueError as exc:
        raise LockedChapterRecoveryError(
            "rehydrated complete-draft Scene pipeline is invalid"
        ) from exc
    return pipeline


def _load_verified_json_artifact(
    metadata: Any,
    *,
    run_dir: Path,
) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    path = _verified_run_artifact_path(metadata, run_dir=run_dir)
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _indexed_scene_mapping(
    values: list[Any],
    *,
    field: str = "index",
) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if index <= 0 or index in result:
            return {}
        result[index] = item
    return result


def _recover_persisted_scene_sequence(
    payload: dict[str, Any],
    *,
    run_dir: Path,
    expected_scene_count: int,
    language: str | None,
) -> list[dict[str, Any]]:
    """Recover a hash-bound final Scene sequence without another model call."""

    run = payload.get("run")
    run_chapter = run.get("chapter") if isinstance(run, dict) else None
    pipeline = (
        run_chapter.get("pipeline")
        if isinstance(run_chapter, dict)
        else None
    )
    artifacts = pipeline.get("artifacts") if isinstance(pipeline, dict) else None
    sources = pipeline.get("scene_sources") if isinstance(pipeline, dict) else None
    spans = pipeline.get("scene_spans") if isinstance(pipeline, dict) else None
    scene_artifacts = (
        artifacts.get("scene_drafts")
        if isinstance(artifacts, dict)
        else None
    )
    continuity_artifact = (
        artifacts.get("scene_continuity")
        if isinstance(artifacts, dict)
        else None
    )
    chapter = payload.get("chapter")
    if (
        not isinstance(chapter, str)
        or not isinstance(scene_artifacts, list)
        or not isinstance(sources, list)
        or not isinstance(spans, list)
        or not isinstance(continuity_artifact, dict)
        or len(scene_artifacts) != expected_scene_count
        or len(sources) != expected_scene_count
        or len(spans) != expected_scene_count
    ):
        return []
    if not _accepted_scene_sequence_integrity(
        run_chapter,
        pipeline,
        chapter_sha256=_content_sha256(chapter),
    ):
        return []
    try:
        validate_language_output(
            chapter,
            CHAPTER_CONTRACT,
            language=language,
        )
    except ModelOutputError:
        return []

    continuity_path = _verified_run_artifact_path(
        continuity_artifact,
        run_dir=run_dir,
    )
    if continuity_path is None:
        return []
    try:
        continuity = json.loads(continuity_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(continuity, dict):
        return []
    try:
        continuity_scenes = {
            int(scene.get("index") or 0): scene
            for scene in continuity.get("scenes") or []
            if isinstance(scene, dict) and int(scene.get("index") or 0) > 0
        }
        source_by_index = {
            int(source.get("scene_index") or 0): source
            for source in sources
            if isinstance(source, dict)
            and int(source.get("scene_index") or 0) > 0
        }
        span_by_index = {
            int(span.get("index") or 0): span
            for span in spans
            if isinstance(span, dict) and int(span.get("index") or 0) > 0
        }
    except (TypeError, ValueError):
        return []
    if (
        set(continuity_scenes) != set(range(1, expected_scene_count + 1))
        or set(source_by_index) != set(range(1, expected_scene_count + 1))
        or set(span_by_index) != set(range(1, expected_scene_count + 1))
    ):
        return []

    recovered: list[dict[str, Any]] = []
    expected_span_start = 0
    for index, metadata in enumerate(scene_artifacts, start=1):
        if not isinstance(metadata, dict):
            return []
        artifact_path = _verified_run_artifact_path(metadata, run_dir=run_dir)
        if artifact_path is None:
            return []
        try:
            artifact_text = artifact_path.read_text(encoding="utf-8-sig")
        except OSError:
            return []
        _header, separator, body = artifact_text.partition("\n---\n\n")
        if not separator:
            return []
        source = source_by_index[index]
        expected_text_sha256 = str(source.get("text_sha256") or "")
        candidates = [body]
        if body.endswith("\n"):
            candidates.append(body[:-1])
        scene_text = next(
            (
                candidate
                for candidate in candidates
                if _content_sha256(candidate) == expected_text_sha256
            ),
            None,
        )
        if scene_text is None:
            return []
        span = span_by_index[index]
        try:
            span_chars = int(span.get("chars") or 0)
            span_start = int(span.get("start_char") or 0)
            span_end = int(span.get("end_char") or 0)
        except (TypeError, ValueError):
            return []
        if (
            span_start != expected_span_start
            or span_end - span_start != len(scene_text)
            or span_chars != len(scene_text)
            or chapter[span_start:span_end] != scene_text
        ):
            return []
        expected_span_start = span_end
        if index < expected_scene_count:
            if chapter[expected_span_start:expected_span_start + 2] != "\n\n":
                return []
            expected_span_start += 2
        elif expected_span_start != len(chapter):
            return []
        source_provenance = _verified_scene_source_from_run(
            run_dir,
            run,
            source,
        )
        if source_provenance is None:
            return []
        continuity_scene = continuity_scenes[index]
        raw_deltas = continuity_scene.get("deltas")
        if not isinstance(raw_deltas, dict):
            return []
        source_response = _verified_persisted_scene_response(
            run_dir,
            source_provenance,
            language=language,
        )
        if (
            source_response is None
            or source_response.get("text") != scene_text
            or source_response.get("events") != continuity_scene.get("events")
            or source_response.get("deltas") != raw_deltas
            or source_response.get("continuity_note")
            != continuity_scene.get("continuity_note")
        ):
            return []
        recovered.append(
            {
                "index": index,
                "text": scene_text,
                "sha256": expected_text_sha256,
                "source_attempt_id": source_provenance["attempt_id"],
                "source_provenance": source_provenance,
                "events": [
                    dict(event)
                    for event in continuity_scene.get("events") or []
                    if isinstance(event, dict)
                ],
                "deltas": {
                    key: [
                        dict(item)
                        for item in raw_deltas.get(key) or []
                        if isinstance(item, dict)
                    ]
                    for key in (
                        "characters",
                        "relationships",
                        "rosters",
                        "locations",
                        "inventory",
                        "counters",
                    )
                },
                "continuity_note": str(
                    continuity_scene.get("continuity_note") or ""
                ),
            }
        )
    merged = "\n\n".join(scene["text"] for scene in recovered)
    if merged != chapter:
        return []
    return recovered


def _accepted_scene_sequence_integrity(
    run_chapter: dict[str, Any],
    pipeline: dict[str, Any],
    *,
    chapter_sha256: str,
) -> bool:
    """Prove the recovered Scene sequence against the latest available gate.

    A declared final artifact is authoritative and must be accepted.  When a
    run stopped before that gate existed, an accepted merge report may prove
    only that the persisted Scene sequence reconstructs the merged chapter;
    the next normal run still performs full validation and the final gate.
    """

    integrity = pipeline.get("integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    final_candidates: list[tuple[Any, str]] = []
    for container, field in (
        (pipeline, "final_artifact"),
        (run_chapter, "final_artifact"),
        (integrity, "final_gate"),
    ):
        if field in container:
            final_candidates.append((container.get(field), "artifact_sha256"))
    for records in (
        pipeline.get("integrity_records"),
        run_chapter.get("integrity_records"),
    ):
        if not isinstance(records, list):
            continue
        final_candidates.extend(
            (record, "output_sha256")
            for record in records
            if isinstance(record, dict) and record.get("stage") == "final_gate"
        )
    if final_candidates:
        return all(
            _accepted_integrity_candidate(
                candidate,
                chapter_sha256=chapter_sha256,
                required_hash_field=required_hash_field,
            )
            for candidate, required_hash_field in final_candidates
        )

    if "merge" not in integrity:
        return False
    merge_candidates: list[tuple[Any, str]] = [
        (integrity.get("merge"), "artifact_sha256"),
    ]
    for records in (
        pipeline.get("integrity_records"),
        run_chapter.get("integrity_records"),
    ):
        if not isinstance(records, list):
            continue
        merge_candidates.extend(
            (record, "output_sha256")
            for record in records
            if isinstance(record, dict) and record.get("stage") == "merge"
        )
    return bool(merge_candidates) and all(
        _accepted_integrity_candidate(
            candidate,
            chapter_sha256=chapter_sha256,
            required_hash_field=required_hash_field,
        )
        for candidate, required_hash_field in merge_candidates
    )


def _accepted_integrity_candidate(
    candidate: Any,
    *,
    chapter_sha256: str,
    required_hash_field: str,
) -> bool:
    if not isinstance(candidate, dict):
        return False
    if candidate.get("accepted") is not True:
        return False
    required_hash = candidate.get(required_hash_field)
    if not isinstance(required_hash, str) or required_hash != chapter_sha256:
        return False
    declared_hashes = [
        str(candidate[field])
        for field in ("artifact_sha256", "output_sha256")
        if isinstance(candidate.get(field), str) and candidate.get(field)
    ]
    return bool(declared_hashes) and all(
        value == chapter_sha256 for value in declared_hashes
    )


def _verified_run_artifact_path(
    metadata: dict[str, Any],
    *,
    run_dir: Path,
) -> Path | None:
    expected_sha256 = str(metadata.get("sha256") or "")
    raw_path = metadata.get("path")
    if len(expected_sha256) != 64 or not isinstance(raw_path, str):
        return None
    try:
        root = run_dir.resolve(strict=True)
        path = Path(raw_path).resolve(strict=True)
        path.relative_to(root)
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        return None
    return path


def _verified_scene_source_from_run(
    run_dir: Path,
    run: Any,
    source: dict[str, Any],
) -> dict[str, str] | None:
    declared = source.get("source_provenance")
    if declared is not None:
        source_chapter_index = (
            int(run.get("chapter_index") or 0)
            if isinstance(run, dict)
            else None
        )
        source_book_id = _run_book_id(run) if isinstance(run, dict) else None
        try:
            verified = verify_scene_source_provenance(
                run_dir,
                declared,
                expected_chapter_index=source_chapter_index,
                expected_book_id=source_book_id,
            )
        except SceneSourceProvenanceError:
            return None
        source_attempt_id = str(source.get("source_attempt_id") or "").strip()
        source_call_id = str(source.get("source_call_id") or "").strip()
        if (
            source_attempt_id
            and source_attempt_id != verified["attempt_id"]
        ) or (
            source_call_id
            and source_call_id != verified["call_id"]
        ):
            return None
        return verified
    if not isinstance(run, dict):
        return None
    evidence = run.get("execution_evidence")
    execution_id = (
        str(evidence.get("execution_id") or "")
        if isinstance(evidence, dict)
        else ""
    )
    source_run_id = str(run.get("id") or "")
    attempt_id = str(source.get("source_attempt_id") or "").strip() or None
    call_id = str(source.get("source_call_id") or "").strip() or None
    try:
        return verified_scene_source_provenance(
            run_dir,
            source_run_id=source_run_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            call_id=call_id,
        )
    except SceneSourceProvenanceError:
        return None


def _verified_persisted_scene_response(
    run_dir: Path,
    source_provenance: dict[str, str],
    *,
    language: str | None,
) -> dict[str, Any] | None:
    try:
        response = verified_scene_source_response(
            run_dir,
            source_provenance,
            language=language,
        )
    except SceneSourceProvenanceError:
        return None
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("events"), list)
        or not isinstance(response.get("deltas"), dict)
        or "continuity_note" not in response
    ):
        # Legacy plain-prose responses may prove their own text, but they cannot
        # authorize structured events or state deltas added later.
        return None
    return response


def _source_execution_dir(run_dir: Path, source: dict[str, Any]) -> Path | None:
    value = source.get("execution_dir")
    if isinstance(value, Path) and value.is_dir():
        return value
    run = source.get("payload", {}).get("run", {})
    evidence = run.get("execution_evidence") if isinstance(run, dict) else None
    execution_id = evidence.get("execution_id") if isinstance(evidence, dict) else None
    if not isinstance(execution_id, str) or not execution_id.startswith("execution_"):
        return None
    candidate = run_dir / "executions" / execution_id
    return candidate if candidate.is_dir() else None


def _latest_usable_complete_transform(
    execution_dir: Path | None,
    *,
    language: str | None,
    excluded_stages: set[str] | None = None,
) -> dict[str, Any] | None:
    if execution_dir is None:
        return None
    store = ModelCallStore(execution_dir / "model_calls")
    if not store.intents_dir.is_dir():
        return None
    candidates: list[dict[str, Any]] = []
    excluded = excluded_stages or set()
    for path in sorted(store.intents_dir.glob("*.json")):
        intent = store.load_intent(path.stem)
        if (
            intent.get("stage") not in {"claude_polish", "scene_repair"}
            or intent.get("stage") in excluded
        ):
            continue
        attempt_id = str(intent["attempt_id"])
        if not store.has_receipt(attempt_id):
            continue
        receipt = store.load_receipt(attempt_id)
        if receipt.get("status") != "succeeded" or not receipt.get("response_artifact_ref"):
            continue
        candidates.append({"intent": intent, "receipt": receipt})
    candidates.sort(
        key=lambda item: (
            str(item["intent"].get("created_at") or ""),
            str(item["intent"].get("attempt_id") or ""),
        ),
        reverse=True,
    )
    for item in candidates:
        try:
            text = _read_verified_response(store, item["receipt"])
            text = validate_language_output(text, CHAPTER_CONTRACT, language=language)
        except (LockedChapterRecoveryError, ModelOutputError, OSError):
            continue
        if len(text.strip()) < 500:
            continue
        intent = item["intent"]
        return {
            "text": text,
            "sha256": _content_sha256(text),
            "problem_codes": [],
            "source_stage": str(intent["stage"]),
            "source_attempt_id": str(intent["attempt_id"]),
        }
    return None


def _repair_did_not_converge(payload: dict[str, Any]) -> bool:
    run = payload.get("run")
    error = run.get("error") if isinstance(run, dict) else None
    if not isinstance(error, dict):
        return False
    error_type = str(error.get("type") or "")
    message = str(error.get("message") or "").lower()
    return error_type == "RepairNotConvergingError" or any(
        marker in message
        for marker in (
            "chapter_repair_not_converging",
            "repair validation did not converge",
        )
    )


def _structured_scene_prefix_is_safe_fallback(payload: dict[str, Any]) -> bool:
    """Do not let raw responses bypass contradictory persisted evidence.

    A pipeline-less interruption has no later Scene claim to contradict.  A
    non-converging repair is the other deliberate exception: its persisted
    pipeline may describe the rejected repaired candidate, while the original
    receipt-backed Scene sequence remains the trusted input to revalidation.
    """

    if _repair_did_not_converge(payload):
        return True
    run = payload.get("run")
    run_chapter = run.get("chapter") if isinstance(run, dict) else None
    pipeline_candidates = (
        run_chapter.get("pipeline") if isinstance(run_chapter, dict) else None,
        run.get("chapter_pipeline") if isinstance(run, dict) else None,
    )
    return not any(isinstance(candidate, dict) for candidate in pipeline_candidates)


def _inherit_active_draft_provenance(
    complete_draft: dict[str, Any] | None,
    active_checkpoint: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(complete_draft, dict) or not isinstance(active_checkpoint, dict):
        return complete_draft
    active_draft = active_checkpoint.get("complete_draft")
    if not isinstance(active_draft, dict):
        return complete_draft
    if active_draft.get("sha256") != complete_draft.get("sha256"):
        return complete_draft
    if active_draft.get("source_stage") not in {"claude_polish", "scene_repair"}:
        return complete_draft
    return dict(active_draft)


def _recover_scene_prefix(
    execution_dir: Path,
    *,
    active_checkpoint: dict[str, Any] | None,
    expected_scene_count: int,
    language: str | None,
    source_run_id: str,
    expected_chapter_index: int,
    expected_book_id: str | None,
) -> list[dict[str, Any]]:
    scenes = [dict(scene) for scene in (active_checkpoint or {}).get("scenes", [])]
    seen_attempts = {str(scene["source_attempt_id"]) for scene in scenes}
    store = ModelCallStore(execution_dir / "model_calls")
    intents: list[dict[str, Any]] = []
    if store.intents_dir.is_dir():
        for path in sorted(store.intents_dir.glob("*.json")):
            intent = store.load_intent(path.stem)
            if intent["stage"] == "chapter_generation":
                intents.append(intent)
    intents.sort(key=lambda item: (str(item["created_at"]), str(item["attempt_id"])))
    indexed_intents = [
        intent
        for intent in intents
        if parse_scene_generation_call_id(intent.get("call_id")) is not None
    ]
    if indexed_intents:
        # Current-format scene calls carry their logical scene identity. This
        # excludes unrelated chapter-plan calls that share the legacy stage.
        intents = indexed_intents

    for intent in intents:
        attempt_id = str(intent["attempt_id"])
        if attempt_id in seen_attempts or not store.has_receipt(attempt_id):
            continue
        receipt = store.load_receipt(attempt_id)
        if receipt["status"] != "succeeded" or not receipt.get("response_artifact_ref"):
            continue
        try:
            response = _recover_scene_response(
                _read_verified_response(store, receipt),
                language=language,
            )
        except (LockedChapterRecoveryError, OSError):
            break
        if response is None:
            break
        text = response["text"]
        if len(text) < 100:
            break
        identity = parse_scene_generation_call_id(intent.get("call_id"))
        scene_index = (
            int(identity["scene_index"])
            if identity is not None
            else len(scenes) + 1
        )
        if scene_index > expected_scene_count:
            break
        try:
            source_provenance = verified_scene_source_provenance(
                execution_dir.parent.parent,
                source_run_id=source_run_id,
                execution_id=execution_dir.name,
                attempt_id=attempt_id,
                call_id=str(intent["call_id"]),
                expected_chapter_index=expected_chapter_index,
                expected_book_id=expected_book_id,
            )
        except SceneSourceProvenanceError:
            break
        candidate = {
            "index": scene_index,
            **response,
            "sha256": _content_sha256(text),
            "source_attempt_id": attempt_id,
            "source_provenance": source_provenance,
        }
        if identity is None:
            scenes.append(candidate)
        elif int(identity["boundary_retry"]) > 0:
            if scene_index > len(scenes):
                # A boundary retry is only meaningful when its rejected
                # primary/recovered candidate is already present.
                break
            scenes[scene_index - 1] = candidate
        elif scene_index == len(scenes) + 1:
            scenes.append(candidate)
        else:
            # A primary call must advance the contiguous prefix exactly once.
            # Replaying or backfilling a primary would make ownership unclear.
            break
        seen_attempts.add(attempt_id)
    return scenes


def _recover_scene_response(
    raw: str,
    *,
    language: str | None,
) -> dict[str, Any] | None:
    return normalize_scene_response(raw, language=language)


def _read_verified_response(store: ModelCallStore, receipt: dict[str, Any]) -> str:
    relative = Path(str(receipt["response_artifact_ref"]))
    path = (store.root / relative).resolve(strict=True)
    try:
        path.relative_to(store.root)
    except ValueError as exc:
        raise LockedChapterRecoveryError("model response artifact escaped its evidence directory") from exc
    raw = path.read_bytes()
    if model_response_artifact_hash(raw) != receipt["response_artifact_hash"]:
        raise LockedChapterRecoveryError("model response artifact hash mismatch")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockedChapterRecoveryError("model response artifact is not UTF-8 text") from exc


def _expected_scene_count(run: dict[str, Any]) -> int:
    run_chapter = run.get("chapter")
    pipeline_candidates = [
        run_chapter.get("pipeline") if isinstance(run_chapter, dict) else None,
        run.get("chapter_pipeline"),
    ]
    for pipeline in pipeline_candidates:
        if not isinstance(pipeline, dict):
            continue
        plan = pipeline.get("plan")
        planned_scenes = plan.get("scenes") if isinstance(plan, dict) else None
        if isinstance(planned_scenes, list) and planned_scenes:
            return len(planned_scenes)
        count = pipeline.get("scene_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return count
    story_metadata = ((run.get("input_pack") or {}).get("metadata") or {}).get("story_project")
    count = (
        story_metadata.get("planned_scene_count")
        if isinstance(story_metadata, dict)
        else None
    )
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    count = (
        story_metadata.get("required_beat_count")
        if isinstance(story_metadata, dict)
        else None
    )
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise LockedChapterRecoveryError("locked run does not record a valid StoryProject scene count")
    return count


def _run_book_id(run: dict[str, Any]) -> str | None:
    direct = run.get("book_id")
    if isinstance(direct, str) and direct:
        return direct
    story = run.get("story_project")
    value = story.get("book_id") if isinstance(story, dict) else None
    return str(value) if isinstance(value, str) and value else None


def _run_sort_key(payload: dict[str, Any], path: Path) -> tuple[str, int]:
    run = payload["run"]
    return str(run.get("finished_at") or run.get("started_at") or ""), path.stat().st_mtime_ns


def _public_result(marker: dict[str, Any], marker_path: Path) -> dict[str, Any]:
    scene_count = len(marker["scenes"])
    expected_scene_count = int(marker["expected_scene_count"])
    return {
        "ok": True,
        "status": "recovered",
        "chapter_index": marker["chapter_index"],
        "action": marker["action"],
        "reason": marker["reason"],
        "reusable_scene_count": scene_count,
        "expected_scene_count": expected_scene_count,
        "next_scene_index": (
            min(scene_count + 1, expected_scene_count)
            if marker["action"] == "resume_scenes"
            else None
        ),
        "resolved_execution_count": len(marker["resolved_execution_ids"]),
        "source_run_id": marker["source_run_id"],
        "draft_stage": (
            marker["complete_draft"].get("source_stage")
            if isinstance(marker.get("complete_draft"), dict)
            else None
        ),
        "checkpoint_path": str(marker_path.resolve()),
        "provider_calls": 0,
    }


def _already_recovered_result(
    chapter_index: int,
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    if checkpoint is None:
        return {
            "ok": True,
            "status": "not_locked",
            "chapter_index": chapter_index,
            "action": "none",
            "reusable_scene_count": 0,
            "expected_scene_count": 0,
            "next_scene_index": None,
            "resolved_execution_count": 0,
            "checkpoint_path": None,
            "provider_calls": 0,
        }
    result = _public_result(checkpoint, Path(checkpoint["_path"]))
    result["status"] = "already_recovered"
    result["resolved_execution_count"] = 0
    return result


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "LockedChapterRecoveryError",
    "load_verified_locked_chapter_pipeline",
    "recover_locked_chapter",
    "verified_final_recovery_draft",
]
