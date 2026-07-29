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
from core.state.snapshot import load_snapshot
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
            complete_draft = _usable_complete_draft(source_payload, language=language)
        elif force_reset and active_checkpoint is not None:
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
                    "the active recovery checkpoint source run is unavailable; refusing an unbound reset"
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
        if recovered_scenes:
            complete_draft = None
        elif not force_reset:
            durable_transform = _latest_usable_complete_transform(
                execution_dir,
                language=language,
            )
            if durable_transform is not None:
                complete_draft = durable_transform
            else:
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
            continue
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
        complete_draft = _usable_complete_draft(item["payload"], language=language)
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


def _usable_complete_draft(payload: dict[str, Any], *, language: str | None) -> dict[str, Any] | None:
    value = payload.get("chapter")
    if not isinstance(value, str) or len(value.strip()) < 500:
        return None
    try:
        text = validate_language_output(value, CHAPTER_CONTRACT, language=language)
    except ModelOutputError:
        return None
    validation = payload.get("validation")
    problem_codes = validation.get("problem_codes") if isinstance(validation, dict) else []
    return {
        "text": text,
        "sha256": _content_sha256(text),
        "problem_codes": [str(code) for code in problem_codes or [] if str(code).strip()],
        "source_stage": "chapter",
    }


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
    final_artifact = (
        pipeline.get("final_artifact")
        if isinstance(pipeline, dict)
        else None
    )
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
        or not isinstance(final_artifact, dict)
        or len(scene_artifacts) != expected_scene_count
        or len(sources) != expected_scene_count
        or len(spans) != expected_scene_count
        or final_artifact.get("accepted") is not True
        or str(final_artifact.get("artifact_sha256") or "") != _content_sha256(chapter)
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
            span_length = int(span.get("end_char") or 0) - int(
                span.get("start_char") or 0
            )
        except (TypeError, ValueError):
            return []
        if span_chars != len(scene_text) or span_length != len(scene_text):
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
) -> dict[str, Any] | None:
    if execution_dir is None:
        return None
    store = ModelCallStore(execution_dir / "model_calls")
    if not store.intents_dir.is_dir():
        return None
    candidates: list[dict[str, Any]] = []
    for path in sorted(store.intents_dir.glob("*.json")):
        intent = store.load_intent(path.stem)
        if intent.get("stage") not in {"claude_polish", "scene_repair"}:
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
    "recover_locked_chapter",
]
